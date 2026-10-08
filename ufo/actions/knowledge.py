"""Search and knowledge: find files, documents and text across the machine.

Backs "find that pdf about taxes" and "search my notes". The searches are
filename- and content-based, which is honest about what they can and cannot see:
nothing here builds a semantic index, and it does not claim to.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger

from . import filesystem

log = get_logger(__name__)

#: Where a user's documents actually live.
SEARCH_ROOTS = ("Desktop", "Documents", "Downloads", "Pictures", "Music")


def _roots(target: str = "") -> list[Path]:
    if target:
        return [Path(filesystem.expand(target))]
    home = Path.home()
    return [home / name for name in SEARCH_ROOTS if (home / name).is_dir()]


def search_files(target: str = "", args: str = "") -> str:
    """Find files by name pattern. target is the pattern, args the folder."""
    pattern = target or "*"
    root = args or str(Path.home())
    hits = filesystem.search(root, pattern=pattern)
    if not hits:
        return f"no files matching {pattern!r} under {root}"
    return "\n".join(str(h) for h in hits[:40])


def find_document(target: str = "", args: str = "") -> str:
    """Find a document by words in its name.

    Scores by how many of the query words appear, so "taxes 2024" beats a file
    merely called "taxes", and a lone weak match is refused rather than guessed.
    """
    query = f"{target} {args}".strip().lower()
    if not query:
        return "say what to look for"
    words = [w for w in re.findall(r"[a-z0-9]+", query) if len(w) > 2]
    if not words:
        return "give me at least one word to search for"
    scored: list[tuple[int, Path]] = []
    for root in _roots():
        for path in filesystem.search(str(root), pattern="*.*", max_depth=5):
            if path.suffix.lower() in (".tmp", ".lnk", ".exe", ".dll"):
                continue
            name = path.stem.lower()
            hits = sum(1 for w in words if w in name)
            if hits:
                scored.append((hits, path))
    if not scored:
        return f"nothing found for {query!r}"
    scored.sort(key=lambda row: (-row[0], str(row[1])))
    best, path = scored[0]
    if best < 2 and len(words) > 1 and len(scored) > 1:
        options = "\n".join(f"  {p}" for _s, p in scored[:6])
        return (f"several files could match {query!r}. Which one?\n{options}")
    others = "\n".join(f"  {p}" for _s, p in scored[1:6])
    return f"{path}" + (f"\n\nother matches:\n{others}" if others else "")


def search_documents(target: str = "", args: str = "") -> str:
    """Search inside document text, not just filenames."""
    query = f"{target} {args}".strip()
    if not query:
        return "say what to look for"
    from . import docs

    hits = []
    for root in _roots():
        for path in filesystem.search(str(root), pattern="*.txt"):
            text = filesystem.read_text(str(path))
            if query.lower() in text.lower():
                hits.append(path)
    return "\n".join(str(h) for h in hits[:30]) or f"nothing contains {query!r}"


def search_everything(target: str = "", args: str = "") -> str:
    """One search across filenames and text, best first."""
    return (f"{find_document(target, args)}\n\n"
            f"by filename:\n{search_files('*' + target.strip() + '*', args)}")


def search_images(target: str = "", args: str = "") -> str:
    hits = filesystem.search(str(Path.home()), pattern="*.jpg")
    if not target:
        return "\n".join(str(h) for h in hits[:30]) or "no images found"
    needle = target.lower()
    matched = [h for h in hits if needle in h.stem.lower()]
    return "\n".join(str(h) for h in matched[:30]) or f"no image matching {target!r}"


def index_folder(target: str = "", args: str = "") -> str:
    """Record a folder's file list so later searches know where to look."""
    root = Path(filesystem.expand(target or str(Path.home())))
    if not root.is_dir():
        return f"no such folder: {root}"
    files = [p for p in filesystem.search(str(root), pattern="*.*", max_depth=6)]
    index = {"root": str(root), "files": [str(p) for p in files]}
    path = Path.home() / ".ufo" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=1), encoding="utf-8")
    return f"indexed {len(files)} file(s) under {root}"


def semantic_search(target: str = "", args: str = "") -> str:
    """Say plainly that this is lexical, not semantic."""
    return ("search here is by words in names and text, not meaning. "
            "Give me keywords and I will find the file.")


def recall(target: str = "", args: str = "") -> str:
    """What the agent remembers about a subject."""
    from ..config import active
    from ..core.memory import get_memory

    subject = f"{target} {args}".strip()
    value = get_memory().recall(subject)
    return value or f"nothing remembered about {subject!r}"


def remember(target: str = "", args: str = "") -> str:
    subject = target.strip()
    value = args.strip()
    if not subject or not value:
        return 'give me "subject" and what to remember'
    from ..core.memory import get_memory

    get_memory().remember(subject, value)
    return f"remembered {subject!r}"


def forget(target: str = "", args: str = "") -> str:
    from ..core.memory import get_memory

    subject = (target or args).strip()
    get_memory().forget(subject)
    return f"forgot {subject!r}"


def recall_recent(target: str = "", args: str = "") -> str:
    """Recent episodes, newest first. This is what "what did I just do" means."""
    from ..core.memory import get_memory

    subject = f"{target} {args}".strip()
    memory = get_memory()
    if subject:
        return "\n".join(f"{e.goal}" for e in memory.similar(subject, limit=5)) \
            or f"nothing recent about {subject!r}"
    episodes = memory.episodes(limit=10)
    if not episodes:
        return "no recorded runs yet"
    return "\n".join(f"{getattr(e, 'at', '')} {e.goal}" for e in episodes)


def web_lookup(target: str = "", args: str = "") -> str:
    """Fetch a page's text so it can be read without a browser."""
    from . import network

    url = (target or args).strip()
    if not url.startswith("http"):
        url = f"https://{url}"
    try:
        return network.fetch_text(url)[:4000]
    except Exception as e:
        return f"could not read {url}: {type(e).__name__}: {e}"
