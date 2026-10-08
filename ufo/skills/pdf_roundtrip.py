"""Deterministic, one-command PDF round trip through iLovePDF.

Say (or type) something like:

    convert test.pdf to word, change 2024 to 2025, convert it back to pdf

and this does the whole thing with no model in the loop:

    1. upload the PDF to ilovepdf.com/pdf_to_word   (Selenium, real file input)
    2. wait for the conversion, download the .docx
    3. edit the .docx in place with python-docx    (exact, no GUI)
    4. upload the .docx to ilovepdf.com/word_to_pdf
    5. wait, download the .pdf
    6. verify the finished PDF really contains the new text

Why this is a script and not an agent loop: the flow is long, every step is
known, and the steps that can wait do wait for a real condition rather than a
guess. A small local model improvising twenty browser steps takes tens of
minutes and fails halfway, leaving a half-converted file behind. Here the only
thing the model is ever asked for is which text to replace.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..logging_setup import get_logger, record

log = get_logger(__name__)

ILOVEPDF_PDF_TO_WORD = "https://www.ilovepdf.com/pdf_to_word"
ILOVEPDF_WORD_TO_PDF = "https://www.ilovepdf.com/word_to_pdf"

# The site is the slow, unreliable part, so every wait is generous. These are
# ceilings, not sleeps: the flow moves on the moment the real condition holds.
PAGE_LOAD_SECONDS = 60
UPLOAD_SETTLE_SECONDS = 20
CONVERT_SECONDS = 240
DOWNLOAD_SECONDS = 120

MIN_RESULT_BYTES = 512


class RoundtripError(RuntimeError):
    """A stage failed. The message is meant to be read out loud."""


# --------------------------------------------------------------------------
@dataclass
class RoundtripResult:
    source: Path
    docx: Path
    output: Path
    replacements: int
    verified_text: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.output.exists() and self.replacements > 0)

    def describe(self) -> str:
        if not self.ok:
            return f"Failed: no converted file was produced at {self.output}."
        return (f"Done. {self.replacements} replacement(s) in the Word file. "
                f"The new PDF is at {self.output}.")


# --------------------------------------------------------------------------
def _require(module: str, hint: str):
    try:
        return __import__(module)
    except ImportError as e:
        raise RoundtripError(
            f"{hint} is not installed. Run: pip install {module}") from e


def make_driver(download_dir: Path, headless: bool = True):
    """A Chrome instance that downloads straight into `download_dir`.

    Downloads are the one part of the flow that cannot be verified by looking
    at the page, so the directory is watched instead: a file only counts once
    it has stopped growing.
    """
    _require("selenium", "selenium")
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    download_dir.mkdir(parents=True, exist_ok=True)
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,1100")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("prefs", {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        # A PDF must land on disk as a file, not open in Chrome's viewer.
        "plugins.always_open_pdf_externally": True,
    })
    try:
        return webdriver.Chrome(service=Service(), options=options)
    except Exception as e:
        raise RoundtripError(
            "Could not start Chrome for the conversion. Selenium downloads a "
            f"matching chromedriver on first use, so this usually means no "
            f"network. Original error: {e}") from e


def _wait_for(driver, selector: str, timeout: int, note: str):
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as ec
    from selenium.webdriver.support.ui import WebDriverWait

    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.5).until(
            ec.presence_of_element_located((By.CSS_SELECTOR, selector)))
    except TimeoutException as e:
        raise RoundtripError(
            f"Gave up waiting for {note} after {timeout}s. The converter's "
            f"page may have changed, or it may be rate-limiting uploads.") from e


def _wait_for_download(directory: Path, before: set[Path],
                       suffix: str, timeout: int) -> Path:
    """Wait for a *finished* file: present, and no longer being written.

    A download in progress is a `.crdownload`, and a file that is still
    growing is not the document. Either would hand a corrupt file to the next
    stage, so both are rejected. A file counts as new if it was not there
    before, or if an existing one of the same name was rewritten — re-running
    the flow downloads the same name again.
    """
    seen = {p: p.stat().st_mtime for p in before if p.exists()}
    deadline = time.time() + timeout
    candidate: Path | None = None
    last_size = -1
    stable = 0
    while time.time() < deadline:
        fresh = []
        for path in directory.glob(f"*{suffix}"):
            if path.suffix.lower() != suffix:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if path not in before or seen.get(path) != mtime:
                fresh.append(path)
        if fresh:
            candidate = max(fresh, key=lambda p: p.stat().st_mtime)
            try:
                size = candidate.stat().st_size
            except OSError:
                size = -1
            if size == last_size and size > 0:
                stable += 1
                if stable >= 2:
                    return candidate
            else:
                stable = 0
            last_size = size
        time.sleep(1.0)
    if candidate is not None and candidate.exists() and candidate.stat().st_size > 0:
        return candidate
    raise RoundtripError(
        f"No .{suffix} file appeared in {directory} within {timeout}s.")


def _snapshot_dir(directory: Path) -> set[Path]:
    """Record what is already in the download folder.

    The folder is deliberately *not* emptied. The Word file produced by the
    first conversion is the input to the second one and lives in the same
    place, so clearing it deleted the file the next stage needed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    return {p for p in directory.glob("*") if p.is_file()}


# --------------------------------------------------------------------------
def convert_file(driver, source: Path, url: str, download_dir: Path,
                 stage: str) -> Path:
    """Upload one file to iLovePDF, convert it, download the result."""
    from selenium.webdriver.common.by import By

    if not source.exists():
        raise RoundtripError(f"{stage}: {source} does not exist.")

    before = _snapshot_dir(download_dir)
    driver.get(url)
    _wait_for(driver, "input[type=file]", PAGE_LOAD_SECONDS,
              "the file picker on the converter page")
    time.sleep(2)                          # let the site's scripts attach

    upload = driver.find_element(By.CSS_SELECTOR, "input[type=file]")
    upload.send_keys(str(source))
    log.info("%s: uploaded %s", stage, source.name)
    time.sleep(UPLOAD_SETTLE_SECONDS)

    convert = _wait_for(driver, "#processTask", UPLOAD_SETTLE_SECONDS * 2,
                        "the convert button")
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});",
                          convert)
    convert.click()
    record("skill_stage", stage=stage, action="convert", file=str(source))
    log.info("%s: conversion started", stage)

    suffix = ".docx" if "pdf_to_word" in url else ".pdf"
    # The page swaps to /download/... only once the conversion has finished.
    _wait_for(driver, "a#pickfiles.downloader__btn", CONVERT_SECONDS,
              "the download button")
    link = driver.find_element(By.CSS_SELECTOR, "a#pickfiles.downloader__btn")
    driver.execute_script("arguments[0].click();", link)

    result = _wait_for_download(download_dir, before, suffix, DOWNLOAD_SECONDS)
    if result.stat().st_size < MIN_RESULT_BYTES:
        raise RoundtripError(
            f"{stage}: the downloaded file is only {result.stat().st_size} "
            f"bytes, which is too small to be a real document.")
    log.info("%s: downloaded %s (%d bytes)", stage, result.name,
             result.stat().st_size)
    return result


# --------------------------------------------------------------------------
def edit_docx(path: Path, old: str, new: str, match_case: bool = False) -> int:
    """Replace `old` with `new` everywhere in the document, and say how many.

    Returns the count rather than succeeding quietly: a replacement that hit
    nothing is the single most useful thing to report, because it means the
    converted Word file did not contain the text the user asked to change.
    """
    docx = _require("docx", "python-docx")
    if not old:
        raise RoundtripError("Nothing to replace: the old text was empty.")

    document = docx.Document(str(path))
    count = 0
    flags = 0 if match_case else re.IGNORECASE

    def swap(text: str) -> tuple[str, int]:
        nonlocal count
        found = len(re.findall(re.escape(old), text, flags))
        if found:
            count += found
            text = re.sub(re.escape(old), new.replace("\\", "\\\\"), text, flags=flags)
        return text, found

    for paragraph in document.paragraphs:
        replaced, _ = swap(paragraph.text)
        if replaced != paragraph.text:
            _rewrite_paragraph(paragraph, replaced)

    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    replaced, _ = swap(paragraph.text)
                    if replaced != paragraph.text:
                        _rewrite_paragraph(paragraph, replaced)

    for section in document.sections:
        for part in (section.header, section.footer):
            for paragraph in part.paragraphs:
                replaced, _ = swap(paragraph.text)
                if replaced != paragraph.text:
                    _rewrite_paragraph(paragraph, replaced)

    if count == 0:
        raise RoundtripError(
            f"The text {old!r} does not appear in the converted Word file, so "
            f"nothing was changed. "
            + _suggest_text(old, _all_text(document))
            + " The conversion may have split the text across runs, or the "
              "document may be a scan with no text layer.")

    document.save(str(path))
    record("skill_stage", stage="edit", replacements=count, file=str(path))
    log.info("edit: %d replacement(s) in %s", count, path.name)
    return count


def _all_text(document) -> list[str]:
    lines = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                lines.extend(p.text for p in cell.paragraphs
                             if p.text and p.text.strip())
    return lines


def _suggest_text(old: str, lines: Iterable[str], limit: int = 3) -> str:
    """Offer the closest lines actually in the document.

    A spoken instruction routinely drops punctuation and casing, so the user's
    words are nearly — but not exactly — what is in the file. Naming the real
    lines turns a dead end into a one-word retry, instead of leaving the user
    with no idea what to say. Nothing is changed on the strength of a guess.
    """
    import difflib

    pool = [ln for ln in lines if ln.strip()]
    if not pool or not old:
        return ""
    close = difflib.get_close_matches(old, pool, n=limit, cutoff=0.55)
    if not close:
        head = "; ".join(repr(ln) for ln in pool[:3])
        return f"The document starts with: {head}. "
    listed = "; ".join(repr(ln) for ln in close)
    return (f"Close matches in the document: {listed}. "
            f"Re-run quoting the exact text.")


def _rewrite_paragraph(paragraph, new_text: str) -> None:
    """Put the new text back, keeping the first run's formatting.

    A paragraph's text spans several runs when the converter has styled parts
    of it, and setting `.text` would flatten that formatting. Writing through
    the first run and clearing the rest keeps the paragraph's look.
    """
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(new_text)
        return
    runs[0].text = new_text
    for run in runs[1:]:
        run.text = ""


# --------------------------------------------------------------------------
def verify_pdf(path: Path, expect: str = "") -> str:
    """Confirm the finished PDF is a real, readable document.

    A converter can return an error page saved with a .pdf name. Parsing it and
    looking for the replacement is the only check that the whole chain
    actually produced the document the user asked for.
    """
    pypdf = _require("pypdf", "pypdf")
    if not path.exists():
        raise RoundtripError(f"The final PDF is missing: {path}")
    try:
        reader = pypdf.PdfReader(str(path))
        pages = len(reader.pages)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        raise RoundtripError(
            f"{path.name} is not a readable PDF ({e}). The converter probably "
            f"returned an error page.") from e

    if pages == 0:
        raise RoundtripError(f"{path.name} has no pages.")
    if expect:
        import re as _re
        flags = 0 if expect[0].isupper() and expect[1:].islower() else _re.IGNORECASE
        if not _re.search(_re.escape(expect), text, flags):
            raise RoundtripError(
                f"{path.name} was produced, but {expect!r} is not in its text. "
                f"The edit may not have survived the second conversion.")
    log.info("verify: %s has %d page(s)", path.name, pages)
    return text


# --------------------------------------------------------------------------
def _unique_output(directory: Path, stem: str) -> Path:
    """A result name that will not clobber an earlier run's output.

    Running the same conversion twice is normal, and quietly overwriting the
    first result would destroy work the user may already have opened.
    """
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}_edited.pdf"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_edited_{counter}.pdf"
        counter += 1
    return candidate


def roundtrip(source: str | Path, old: str, new: str,
              out_dir: str | Path | None = None,
              download_dir: str | Path | None = None,
              headless: bool = True, keep_open: bool = False,
              work_dir: str | Path | None = None,
              progress: Callable[[str], None] | None = None) -> RoundtripResult:
    """PDF -> Word -> edit -> PDF, start to finish.

    `progress` is called with a sentence per stage so a voice caller can read
    the steps out loud instead of leaving the user in silence for a minute.
    """
    source = Path(source).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    if not source.exists():
        raise RoundtripError(f"No such file: {source}")
    if source.suffix.lower() != ".pdf":
        raise RoundtripError(
            f"{source.name} is not a PDF. This flow starts from a PDF.")

    base = Path(out_dir).expanduser() if out_dir else source.parent
    base.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir).expanduser() if work_dir else base / ".ufo-roundtrip"
    work.mkdir(parents=True, exist_ok=True)
    downloads = Path(download_dir).expanduser() if download_dir else work / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    stem = source.stem
    final_pdf = _unique_output(base, stem)
    result = RoundtripResult(source=source, docx=work / f"{stem}.docx",
                             output=final_pdf, replacements=0)

    def say(message: str) -> None:
        result.notes.append(message)
        log.info(message)
        if progress:
            try:
                progress(message)
            except Exception:
                pass

    driver = None
    try:
        say(f"Starting Chrome and opening the PDF to Word converter.")
        driver = make_driver(downloads, headless=headless)

        say("Converting the PDF to Word.")
        docx = convert_file(driver, source, ILOVEPDF_PDF_TO_WORD, downloads,
                            "pdf to word")
        result.docx = docx

        say(f"Changing {old!r} to {new!r} in the Word file.")
        result.replacements = edit_docx(docx, old, new)

        say("Converting the edited Word file back to PDF.")
        pdf = convert_file(driver, docx, ILOVEPDF_WORD_TO_PDF, downloads,
                           "word to pdf")
        pdf.replace(final_pdf)
        result.output = final_pdf

        say("Checking the finished PDF.")
        result.verified_text = verify_pdf(final_pdf, new)

        record("skill_complete", source=str(source), output=str(final_pdf),
               replacements=result.replacements)
        say(f"Finished. {final_pdf}")
        return result
    except Exception as e:
        record("skill_failed", source=str(source),
               error=f"{type(e).__name__}: {e}")
        raise
    finally:
        if driver is not None and not keep_open:
            try:
                driver.quit()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Spoken / typed instruction
# --------------------------------------------------------------------------
OLD_NEW_PATTERNS = (
    r"replace\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)(?:$|[,.;]|\band\b)",
    r"change\s+(?P<old>.+?)\s+to\s+(?P<new>.+?)(?:$|[,.;])",
    r"(?P<old>.+?)\s+(?:into|to)\s+(?P<new>.+?)(?:$|[,.;])",
)

_QUOTED = re.compile(r"[\"'\u201c]([^\"'\u201d]{1,120})[\"'\u201d]")

# Speech-to-text turns "replace 2024 with 2025 in the document" into one long
# phrase, so the replacement swallows the rest of the sentence unless it is cut
# back. Only applied to unquoted text: a quoted phrase is exactly what the user
# said and is never trimmed.
_TRAILING = re.compile(
    r"\s+(?:in|inside|within|into|on|for|from|and|then|so|after|before|"
    r"the|a|an|it|document|file|pdf|word|docx|it\s+back|back)\b.*$",
    flags=re.IGNORECASE)


def _clean_phrase(value: str, trim_tail: bool = False) -> str:
    value = value.strip().strip(",.;")
    value = re.sub(r"^(the\s+)?(text|word|string|phrase)\s+", "", value,
                   flags=re.IGNORECASE)
    value = re.sub(r"^(in|from|of)\s+", "", value, flags=re.IGNORECASE)
    if trim_tail:
        value = _TRAILING.sub("", value)
    return value.strip().strip(",.;\"'")


def find_replacement(instruction: str) -> tuple[str, str] | None:
    """Pull `old` and `new` out of a spoken instruction.

    Quoted text wins, because speech-to-text rarely preserves punctuation but
    does preserve the words, and an ambiguous match here would rewrite the
    wrong text in a document the user cares about.
    """
    quoted = _QUOTED.findall(instruction or "")
    if len(quoted) >= 2:
        return _clean_phrase(quoted[0]), _clean_phrase(quoted[1])
    for pattern in OLD_NEW_PATTERNS:
        match = re.search(pattern, instruction or "", flags=re.IGNORECASE)
        if match:
            old = _clean_phrase(match.group("old"), trim_tail=True)
            new = _clean_phrase(match.group("new"), trim_tail=True)
            if old and new:
                return old, new
    return None


def find_pdf(instruction: str, search_dirs: Iterable[Path] = ()) -> Path | None:
    """Find the PDF the user named, searching their folders by name.

    Matching on a name the user actually said, rather than guessing the most
    recent file, keeps this from editing a document they did not mean.
    """
    text = (instruction or "").lower()
    for directory in search_dirs:
        directory = Path(directory).expanduser()
        if not directory.is_dir():
            continue
        best: tuple[int, Path] | None = None
        for pdf in directory.glob("*.pdf"):
            if pdf.name.lower() == text.strip() or text.strip() == pdf.stem.lower():
                return pdf
            score = 0
            for word in re.findall(r"[a-z0-9]{3,}", pdf.stem.lower()):
                if word in text:
                    score += len(word)
            if score and (best is None or score > best[0]):
                best = (score, pdf)
        if best and best[0] >= 4:
            return best[1]
    return None
