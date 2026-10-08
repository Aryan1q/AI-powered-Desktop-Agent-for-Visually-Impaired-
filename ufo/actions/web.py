"""Web and browser: open, search, tabs, bookmarks, download, fill a form.

Everything that needs a page goes through the default browser or a headless
session. Nothing here types a password: `login` opens the page and stops, because
credentials belong in the vault, not in a script.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote_plus

from ..errors import ActionError
from ..logging_setup import get_logger

from . import network, shell

log = get_logger(__name__)


def open_site(target: str = "", args: str = "") -> str:
    """Open a URL, or search for it if it is not one."""
    wanted = (target or args).strip()
    if not wanted:
        return "give me a site or a search"
    if re.match(r"^[a-z]+://", wanted, re.IGNORECASE):
        return shell.open_url(wanted)
    if re.search(r"\.[a-z]{2,}(/|$)", wanted, re.IGNORECASE):
        return shell.open_url(f"https://{wanted}")
    return search(wanted, "")


def search(target: str = "", args: str = "") -> str:
    query = f"{target} {args}".strip()
    if not query:
        return "give me something to search for"
    engine = ("https://duckduckgo.com/?q=" if "duck" in query.lower()
              else "https://www.bing.com/search?q=")
    url = f"{engine}{quote_plus(re.sub(r'^(search|google|bing)\s+', '', query, flags=re.I))}"
    shell.open_url(url)
    return f"searched for {query!r}"


def tabs(target: str = "", args: str = "") -> str:
    """Windows has no supported cross-browser tab API."""
    return ("there is no scriptable cross-browser tab API. Ctrl+Tab switches "
            "windows in Edge and Chrome; Ctrl+Shift+T restores a closed tab.")


def bookmark(target: str = "", args: str = "") -> str:
    """Open the browser's bookmark manager on a site.

    `target` is the site or bookmark and `args` is what to do with it
    (add, list, remove). Only "list" is done here; the other two need the
    browser's own UI, and saying so is better than opening the wrong thing.
    """
    site = str(target or "").strip()
    action = str(args or "").strip().lower()
    if not site:
        site, action = action, ""      # the action arrived alone
    if not site:
        return "say which site's bookmarks you want"

    if action.startswith("list"):
        return ("bookmarks live in the browser profile and I will not rewrite "
                "that file. Open them with Ctrl+Shift+O, or ask me to list the "
                "bookmarks I have saved with `ufo memory`.")
    if action in ("add", "remove", "delete"):
        return (f"to {action} that bookmark, open the bookmark manager "
                f"(Ctrl+Shift+O) and do it there — editing the browser's own "
                f"database while it is running corrupts it.")

    url = site if site.startswith("http") else f"https://{site}"
    browser = _default_browser()
    managers = {"edge": "edge://favorites/", "chrome": "chrome://bookmarks/",
                "firefox": "about:blank"}
    manager = managers.get(browser, managers["edge"])
    shell.open_url(f"{manager}#{url}")
    return f"opened the bookmark manager in {browser}"


def _default_browser() -> str:
    from . import system

    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell"
                            r"\Associations\UrlAssociations\https\UserChoice") as key:
            prog = winreg.QueryValueEx(key, "ProgId")[0]
        return "edge" if "edge" in prog.lower() else (
            "firefox" if "firefox" in prog.lower() else "chrome")
    except Exception:
        return "edge"


def download(target: str = "", args: str = "") -> str:
    """Download a URL to disk. Only http(s), checked by network.fetch.

    `target` is the URL and `args` is where to put it. The destination used to
    be dropped whenever a URL was present, so every download landed in
    Downloads whether or not the user asked for somewhere else.
    """
    url = str(target or "").strip()
    destination = str(args or "").strip()
    if not url:
        url, destination = destination, ""     # the folder arrived alone
    if not url:
        return "give me a URL"
    if not url.startswith("http"):
        url = f"https://{url}"
    name = url.split("/")[-1].split("?")[0] or "download"
    if not destination:
        destination = f"%USERPROFILE%\\Downloads\\{name}"
    elif destination.endswith(("/", "\\")):
        destination = f"{destination}{name}"
    elif not Path(destination).suffix:
        # Keep whichever separator the caller used; joining with a backslash
        # under a forward-slash path reads back wrong in the confirmation.
        sep = "/" if "/" in destination and "\\" not in destination else "\\"
        destination = f"{destination}{sep}{name}"
    result = network.download(url, destination)
    where = destination if destination != f"%USERPROFILE%\\Downloads\\{name}" else "Downloads"
    return f"downloaded {name} to {where}"


def fill_form(target: str = "", args: str = "") -> str:
    """Web forms are typed through the real UI, so the guard applies per field."""
    return ("web form filling is not scriptable safely. Open the page and ask me "
            "to click and type, and the usual allowlist and confirmation apply.")


def login(target: str = "", args: str = "") -> str:
    """Open the sign-in page and stop. Passwords are never typed by a script."""
    site = (target or args).strip()
    if not site:
        return "give me the site to sign in to"
    url = site if site.startswith("http") else f"https://{site}"
    shell.open_url(url)
    return (f"opened {url}. Type the password yourself — I will not put a "
            f"credential into a page without you.")


def purchase(target: str = "", args: str = "") -> str:
    """Find a product. This never completes a purchase."""
    item = str(target or "").strip()
    site = str(args or "").strip()
    if not item:
        item, site = site, ""          # the site arrived alone
    if not item:
        return "say what you are looking for"
    search(f"{item} buy {site}".strip(), "")
    where = f" on {site}" if site else ""
    return (f"searched for {item!r}{where}. I can find and compare, but I will "
            f"not press Buy for you.")


def compare(target: str = "", args: str = "") -> str:
    """Open several shops for the same item in one search."""
    item = str(target or "").strip()
    sites = str(args or "").strip()
    if not item:
        item, sites = sites, ""        # the sites arrived alone
    if not item:
        return "say what to compare"
    search(f"{item} price comparison {sites}".strip(), "")
    return f"opened a price comparison for {item!r}" + (f" across {sites}" if sites else "")


def monitor(target: str = "", args: str = "") -> str:
    """Watch a page for a change. Uses a scheduled task rather than a loop."""
    url = str(target or "").strip()
    watch_for = str(args or "").strip()
    if not url:
        url, watch_for = watch_for, ""
    if not url:
        return "give me the page to watch"
    from . import system

    tail = f" for {watch_for!r}" if watch_for else ""
    return (f"use `ufo schedule add` with {url!r}{tail} instead — a script "
            f"cannot hold a browser open and watch for you")


# --------------------------------------------------------------------------
# page data and privacy
# --------------------------------------------------------------------------
def scrape(target: str = "", args: str = "") -> str:
    """Fetch a page and pull the readable text, links, tables or title out of it.

    `target` is the URL and `args` says what to pull from it. Reading the two
    with `target or args` threw the "links" or "tables" away the moment a URL
    was also given, so the page always came back as plain text.
    """
    from . import network

    url = str(target or "").strip()
    what = str(args or "").strip().lower()
    if not url:
        url, what = what, ""          # the mode arrived alone; no page to read
    if not url:
        return "give me a URL"
    if not url.startswith("http"):
        url = f"https://{url}"
    text = network.fetch_text(url)

    if "link" in what:
        links = re.findall(r'(?i)<a[^>]+href=["\']([^"\']+)["\']', text)
        return "\n".join(links[:100]) or f"{url} had no links"
    if "title" in what:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else f"{url} had no title"
    if "table" in what:
        rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", text)
        out = []
        for row in rows[:60]:
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                    for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
            if any(cells):
                out.append(" | ".join(cells))
        return "\n".join(out) or f"{url} had no tables"

    body = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", text)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:4000] or f"{url} had no readable text"


def clear_data(target: str = "", args: str = "") -> str:
    """Clear cookies, cache or history for one site, or for all of them.

    `target` is *what* to clear ("cookies", "cache", "history", "all") and
    `args` is the browser or site it applies to. The old `target or args` read
    the "what" as if it were a site name.
    """
    what = str(target or "").strip().lower()
    where = str(args or "").strip()

    if what in ("cookies", "cache", "history", "all") or not what:
        if what in ("", "cookies") and where:
            return _clear_site(where)
        return _clear_all(what or "cookies")
    if where:
        return _clear_site(what) if where.lower() in ("", "cookies") else _clear_all(what)
    return _clear_site(what)


def _clear_site(site: str) -> str:
    if not site.startswith("http"):
        site = f"https://{site}"
    for path in ("Network\\Cookies", "Default\\Network\\Cookies"):
        _delete_store(Path(_profile()) / path.split("\\")[0], path, site)
    return f"cleared cookies for {site}"


def _clear_all(what: str) -> str:
    root = Path(_profile()) / "Default"
    if not root.is_dir():
        return f"no browser profile at {root}"
    if what == "cookies":
        removed = 0
        for item in (root / "Network").glob("*Cookies*"):
            try:
                item.unlink()
                removed += 1
            except OSError:
                pass
        return f"cleared {removed} cookie store(s) in {root} (the browser must be closed)"
    if what == "cache":
        cache = root / "Cache"
        removed = 0
        if cache.is_dir():
            for item in cache.rglob("*"):
                if item.is_file():
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        pass
        return f"cleared {removed} cached file(s) in {cache} (the browser must be closed)"
    if what == "history":
        hist = root / "History"
        if hist.exists():
            try:
                hist.unlink()
                return f"deleted the history database at {hist} (the browser must be closed)"
            except OSError as e:
                return f"could not delete {hist}: {e}"
        return f"no history database at {hist}"
    return "say what to clear: cookies, cache, history or all"
    root = Path(_profile()) / "Default" / "Network"
    if not root.is_dir():
        return f"no browser profile at {root}"
    removed = 0
    for item in root.glob("*Cookies*"):
        try:
            item.unlink()
            removed += 1
        except OSError:
            pass
    return f"cleared {removed} cookie store(s) in {root} (the browser must be closed)"


def _profile() -> str:
    root = Path.home() / "AppData/Local"
    for name in ("Google/Chrome/User Data", "Microsoft/Edge/User Data"):
        if (root / name).is_dir():
            return f"{root}/{name}"
    return f"{root}/Microsoft/Edge/User Data"


def _delete_store(base: Path, _sub: str, site: str) -> None:
    """Cookies live in a SQLite file with a lock; delete the file, not the row."""
    folder = base / "Default" / "Network"
    for item in folder.glob("*Cookies*"):
        try:
            item.unlink()
        except OSError as e:
            log.debug("could not remove %s: %s", item, e)


def private(target: str = "", args: str = "") -> str:
    """Open a private window."""
    from . import keyboard

    keyboard.hotkey("ctrl+shift+n")
    return "opened a private window"


def save_as_pdf(target: str = "", args: str = "") -> str:
    """Print the current page to PDF through the browser's own dialog."""
    from . import keyboard

    keyboard.hotkey("ctrl+p")
    return "opened the print dialog — choose Microsoft Print to PDF to save it"


def block_site(target: str = "", args: str = "") -> str:
    """Block or unblock a site.

    `target` is the site and `args` is block or unblock. Both matter: asked to
    unblock, the answer has to be about removing it, not about blocking.
    """
    site = str(target or "").strip()
    action = str(args or "").strip().lower()
    if not site:
        site, action = action, "block"     # the site is what was asked for
    if not site:
        return "give me the site to block"
    if action.startswith("un"):
        return (f"to unblock {site}, remove it from whatever is blocking it: "
                f"the hosts file (see `ufo do adv_hosts`) or the blocker "
                f"extension. I will not rewrite either while the browser is "
                f"running.")
    return ("browsers have no scriptable block list. Use an extension, or "
            f"add {site} to the hosts file (see `ufo do adv_hosts`).")
