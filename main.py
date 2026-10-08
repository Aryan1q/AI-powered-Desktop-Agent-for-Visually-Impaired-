"""UFO — a whole-laptop automation agent for Windows.

Quick start
-----------
    python main.py                            # start it: phone voice control
    python main.py run "type hello world" --allow Notepad
    python main.py doctor                     # check every subsystem
    python main.py --help                     # every command

`python main.py` on its own starts the whole app: it checks that the model, the
transcription engine and the allowlist are ready, prints a URL, and waits for
instructions spoken on a phone on the same Wi-Fi. If something is not ready it
says which thing and the exact command that fixes it, rather than failing later
when you are already holding the phone.

The real CLI lives in `ufo.interface.cli`; this file is the entry point and
keeps the old single-window invocation working:

    python main.py --task "Type hello into the document" --exe notepad.exe --title "Notepad"
    python main.py --task "Reply to the draft email" --allow "Outlook" --allow "Gmail"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ufo.interface.cli import main as cli_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    The legacy flag form (`--task`, `--exe`, `--title`, `--allow`, `--max-steps`
    with no subcommand) is translated into the current `run` subcommand, so old
    scripts and shortcuts keep working.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0].startswith("-"):
        if argv[0] == "--help" or argv[0] == "-h":
            return cli_main(argv)
        argv = ["run"] + argv

    if argv and argv[0] == "run" and "--task" in argv:
        argv = _migrate_legacy_flags(argv)

    return cli_main(argv)


def _migrate_legacy_flags(argv: list[str]) -> list[str]:
    """`run --task X --exe Y` becomes `run "X" --exe Y`."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--task" and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 2
            continue
        if token.startswith("--task="):
            out.append(token.split("=", 1)[1])
            i += 1
            continue
        out.append(token)
        i += 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
