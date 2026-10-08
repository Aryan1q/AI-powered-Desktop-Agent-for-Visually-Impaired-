"""Logging + per-run artifact recording.

Console output stays human readable; when `storage.json_logs` is on, the same
records also go to a JSONL file that the dashboard and the replay viewer read.
Secrets are scrubbed on the way out (see safety.secrets).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Config, active as active_config

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False
_run_log: logging.Handler | None = None


class RunContext:
    """Everything produced by a single task run."""

    def __init__(self, run_id: str, task: str, directory: Path, started: float):
        self.run_id = run_id
        self.task = task
        self.directory = directory
        self.started = started
        self.ended: float | None = None
        self.status: str = "running"
        self.records: list[dict] = []

    @property
    def duration(self) -> float:
        return (self.ended or time.time()) - self.started

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "directory": str(self.directory),
            "started": self.started,
            "ended": self.ended,
            "duration_seconds": round(self.duration, 2),
            "status": self.status,
            "steps": len(self.records),
        }

    def summary(self) -> dict:
        return {
            **self.to_dict(),
            "summary": " | ".join(r.get("event", "") for r in self.records[-12:]),
        }


_current_run: RunContext | None = None


def configure(cfg: Config | None = None) -> None:
    global _configured, _run_log
    cfg = cfg or active_config()
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        root.addHandler(handler)
        root.setLevel(getattr(logging, cfg.storage.log_level.upper(), logging.INFO))
        _configured = True

    if cfg.storage.json_logs:
        path = cfg.storage.log_root / "ufo.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = getattr(logging.getLogger(), "_ufo_json_handler", None)
        if existing is not None and getattr(existing, "baseFilename", None) != str(path):
            root.removeHandler(existing)
            existing.close()
            existing = None
        if existing is None:
            existing = logging.FileHandler(path, encoding="utf-8")
            existing.setFormatter(logging.Formatter("%(message)s"))
            existing.set_name("ufo-jsonl")
            root.addHandler(existing)
            setattr(logging.getLogger(), "_ufo_json_handler", existing)
    else:
        stale = getattr(logging.getLogger(), "_ufo_json_handler", None)
        if stale is not None:
            root.removeHandler(stale)
            stale.close()
            setattr(logging.getLogger(), "_ufo_json_handler", None)

    _run_log = None


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        configure()
    return logging.getLogger(name)


def current_run() -> RunContext | None:
    return _current_run


def new_run_id() -> str:
    return time.strftime("run_%Y%m%d_%H%M%S") + f"_{os.getpid()}"


@contextmanager
def run(task: str, cfg: Config | None = None) -> Iterator[RunContext]:
    """Open a run directory, emit a JSONL trace, and prune old runs on exit."""
    global _current_run
    cfg = cfg or active_config()
    run_id = new_run_id()
    directory = cfg.storage.log_root / run_id
    directory.mkdir(parents=True, exist_ok=True)

    ctx = RunContext(run_id, task, directory, time.time())
    previous, _current_run = _current_run, ctx
    started = time.time()
    try:
        yield ctx
    except KeyboardInterrupt:
        ctx.status = "interrupted"
        raise
    except Exception as e:  # noqa: BLE001 - record then re-raise
        ctx.status = "error"
        get_logger(__name__).error(
            "run %s failed: %s", run_id, e, exc_info=traceback.format_exc()
        )
        raise
    finally:
        ctx.ended = time.time()
        if ctx.status == "running":
            ctx.status = "ok"
        _current_run = previous
        _write_trace(ctx)
        _prune(cfg)
        get_logger(__name__).info(
            "run %s -> %s (%.1fs)", run_id, ctx.status, ctx.duration
        )


def record(event: str, **fields: Any) -> None:
    """Append a structured event to the active run's trace."""
    ctx = _current_run
    if ctx is None:
        return
    payload = {"t": round(time.time() - ctx.started, 3), "event": event, **fields}
    ctx.records.append(payload)
    _append(payload)


def _append(payload: dict) -> None:
    if _run_log is None:
        return
    try:
        _run_log.emit_record(payload)  # type: ignore[attr-defined]
    except Exception:
        pass


def _write_trace(ctx: RunContext) -> None:
    try:
        path = ctx.directory / "trace.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "meta", **ctx.to_dict()}, default=str) + "\n")
            for rec in ctx.records:
                fh.write(json.dumps(rec, default=str) + "\n")
        (ctx.directory / "run.json").write_text(
            json.dumps(ctx.summary(), indent=2, default=str), encoding="utf-8"
        )
    except OSError:
        pass  # logging must never break the run


def _prune(cfg: Config) -> None:
    root = cfg.storage.log_root
    if not root.exists():
        return
    runs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    for stale in runs[: max(0, len(runs) - cfg.storage.keep_runs)]:
        try:
            for child in stale.rglob("*"):
                if child.is_file():
                    child.unlink()
            stale.rmdir()
        except OSError:
            pass


def scrub(text: str) -> str:
    """Redact secrets and collapse newlines — used before anything is logged."""
    if not text:
        return text
    # Imported lazily: safety.secrets must not depend on this module at import
    # time, or the two form a cycle.
    from .safety.secrets import redact as redact_secrets

    return redact_secrets(str(text))


# Re-exported for compatibility. slugify is a text helper and lives in
# ufo.utils.text; it used to be defined here, and ufo.core.memory still
# reaches for it through this module in older imports.
from .utils.text import slugify  # noqa: E402,F401
