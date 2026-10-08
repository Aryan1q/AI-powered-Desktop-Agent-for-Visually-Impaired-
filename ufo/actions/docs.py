"""Documents and Office: create, read, edit, convert, and PDF surgery.

PDF work reuses `skills.pdf_roundtrip`, which already verifies that an edit
actually survived the round trip. The converters here are LibreOffice and
Word's own automation; when one is missing the handler says which package to
install rather than failing three frames down.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem, shell

log = get_logger(__name__)

OFFICE_SUFFIXES = (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".rtf")


def create(target: str = "", args: str = "") -> str:
    """Create an empty document. target is the path, e.g. notes.docx."""
    path = Path(filesystem.expand(target or ""))
    if not path.suffix:
        return "give me a name with an extension, e.g. report.docx"
    if path.suffix.lower() in (".docx", ".txt", ".md"):
        if not path.exists():
            filesystem.write_text(str(path), "")
        shell.open_path(str(path))
        return f"created and opened {path.name}"
    return _libreoffice(f'--writer "{path}"', f"LibreOffice is needed to create {path.name}")


def read(target: str = "", args: str = "") -> str:
    """Read a document's text. Plain files directly, PDFs page by page."""
    path = Path(filesystem.expand(target or ""))
    if not path.is_file():
        return f"no such file: {path}"
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in (".txt", ".md", ".csv", ".json", ".log", ".py", ".yaml", ".yml"):
        return filesystem.read_text(str(path))
    return _convert_to_text(path)


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        raise ActionError("reading a PDF needs pypdf: `pip install pypdf`")
    reader = PdfReader(str(path))
    pages = len(reader.pages)
    out = []
    for number, page in enumerate(reader.pages[:5], start=1):
        out.append(f"--- page {number} of {pages} ---")
        out.append((page.extract_text() or "").strip())
    if pages > 5:
        out.append(f"... and {pages - 5} more page(s)")
    return "\n".join(out)


def _convert_to_text(path: Path) -> str:
    temporary = str(path.with_suffix(".txt"))
    if _libreoffice(f'--convert-to txt --outdir "{path.parent}" "{path}"', ""):
        produced = Path(temporary)
        if produced.is_file():
            text = filesystem.read_text(str(produced))
            produced.unlink()
            return text
    return (f"{path.name} is a {path.suffix} file. I cannot read it as text "
            f"without LibreOffice (`winget install TheDocumentFoundation."
            f"LibreOffice`).")


def _libreoffice(command: str, message: str) -> bool:
    if not shutil.which("soffice"):
        if message:
            raise ActionError(message)
        return False
    shell.run_powershell(f'soffice --headless {command}')
    return True


def edit_text(target: str = "", args: str = "") -> str:
    """Replace text in a document. `args` is 'old|new'."""
    path = Path(filesystem.expand(target or ""))
    old, _, new = args.partition("|")
    if not path.is_file():
        return f"no such file: {path}"
    if not old:
        return "give me 'find|replace'"
    if path.suffix.lower() == ".docx":
        from ..skills.pdf_roundtrip import edit_docx

        count = edit_docx(path, old, new)
        if not count:
            return (f"{old!r} does not appear in {path.name}, so nothing was "
                    f"changed. Nothing was written.")
        return f"replaced {count} occurrence(s) in {path.name}"
    if path.suffix.lower() == ".pdf":
        from ..skills.pdf_roundtrip import roundtrip

        return str(roundtrip(path, old, new, headless=True).describe())
    text = filesystem.read_text(str(path))
    if old not in text:
        return f"{old!r} does not appear in {path.name}, so nothing was changed"
    count = text.count(old)
    filesystem.write_text(str(path), text.replace(old, new))
    return f"replaced {count} occurrence(s) in {path.name}"


def convert(target: str = "", args: str = "") -> str:
    """Convert between document formats through LibreOffice."""
    path = Path(filesystem.expand(target or ""))
    if not path.is_file():
        return f"no such file: {path}"
    if not path.suffix.lower() in OFFICE_SUFFIXES + (".pdf",):
        return f"{path.name} is not a document format I can convert"
    return _libreoffice(
        f'--convert-to --outdir "{path.parent}" "{path}"',
        f"converting {path.name} needs LibreOffice: "
        f"`winget install TheDocumentFoundation.LibreOffice`") and f"converted {path.name}"


def pdf_split(target: str = "", args: str = "") -> str:
    path = Path(filesystem.expand(target or ""))
    if path.suffix.lower() != ".pdf":
        return "give me a PDF"
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore
    except ImportError:
        raise ActionError("splitting a PDF needs pypdf: `pip install pypdf`")
    reader = PdfReader(str(path))
    out = path.with_suffix(".split.pdf")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    with open(out, "wb") as handle:
        writer.write(handle)
    return f"wrote {len(reader.pages)} page(s) to {out.name}"


def pdf_merge(target: str = "", args: str = "") -> str:
    """Merge every PDF in a folder into one, in filename order."""
    folder = Path(filesystem.expand(target or "."))
    files = sorted(folder.glob("*.pdf"))
    if len(files) < 2:
        return f"need at least two PDFs in {folder}, found {len(files)}"
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore
    except ImportError:
        raise ActionError("merging PDFs needs pypdf: `pip install pypdf`")
    writer = PdfWriter()
    for item in files:
        for page in PdfReader(str(item)).pages:
            writer.add_page(page)
    out = folder / "merged.pdf"
    with open(out, "wb") as handle:
        writer.write(handle)
    return f"merged {len(files)} file(s) into {out.name}"


def pdf_extract(target: str = "", args: str = "") -> str:
    """Pull the text out of a PDF."""
    path = Path(filesystem.expand(target or ""))
    if path.suffix.lower() != ".pdf":
        return "give me a PDF"
    return _read_pdf(path)


def form_fill(target: str = "", args: str = "") -> str:
    return ("filling a PDF form is done through the reader UI so you can see each "
            "field. Open the PDF and tell me what to type where.")


def chart(target: str = "", args: str = "") -> str:
    return ("charts need a data source. Give me a CSV path and the columns to "
            "plot, e.g. 'chart sales.csv month;revenue'.")


def mail_merge(target: str = "", args: str = "") -> str:
    return ("mail merge needs Word. Say which document and which recipient list, "
            "and I will set it up in Word so you can review before sending.")


def notes_to_slides(target: str = "", args: str = "") -> str:
    path = Path(filesystem.expand(target or ""))
    if not path.is_file():
        return f"no such file: {path}"
    return _libreoffice(
        f'--convert-to pptx --outdir "{path.parent}" "{path}"',
        "turning notes into slides needs LibreOffice") and f"made {path.stem}.pptx"


def draft(target: str = "", args: str = "") -> str:
    """Start a new document from an outline, opened for editing."""
    lines = [line for line in target or args.splitlines() if line.strip()]
    if not lines:
        return "give me the outline, one point per line"
    path = Path.home() / "Documents" / "outline.md"
    filesystem.write_text(str(path), "\n".join(lines))
    shell.open_path(str(path))
    return f"wrote {len(lines)} point(s) to {path}"
