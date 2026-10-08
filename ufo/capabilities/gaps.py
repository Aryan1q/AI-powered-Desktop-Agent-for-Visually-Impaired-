"""The missing-capability log.

The fallback rule, stated out loud: when nothing in the catalog fits, UFO does
not pretend. It tries the general agent loop, and if that cannot do it either,
it writes down what was asked and why it failed. A week of that log is a
roadmap — it shows which of the 48 domains users actually reach for, and where
the code is thin.

Everything is local: `~/.ufo/gaps.json`, owner-readable, no telemetry.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import ActionError
from ..logging_setup import get_logger
from ..utils import text as textutil

log = get_logger(__name__)

MAX_GAPS = 500
SIMILARITY_MERGE = 0.72


@dataclass
class Gap:
    """One request the agent could not carry out."""

    request: str
    reason: str = ""
    domain: str = ""          # the closest category, when one fits
    attempts: int = 1
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Gap":
        return cls(
            request=str(data.get("request", ""))[:400],
            reason=str(data.get("reason", ""))[:400],
            domain=str(data.get("domain", ""))[:80],
            attempts=int(data.get("attempts", 1) or 1),
            first_seen=float(data.get("first_seen", 0.0) or time.time()),
            last_seen=float(data.get("last_seen", 0.0) or time.time()),
        )

    def describe(self) -> str:
        seen = time.strftime("%Y-%m-%d", time.localtime(self.last_seen))
        times = f"x{self.attempts}" if self.attempts > 1 else ""
        return f"[{seen} {times}] {textutil.truncate(self.request, 60)}" + (
            f"  — {textutil.truncate(self.reason, 70)}" if self.reason else "")


class GapLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._gaps: list[Gap] | None = None

    def load(self) -> list[Gap]:
        if self._gaps is not None:
            return self._gaps
        self._gaps = []
        if not self.path.exists():
            return self._gaps
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("could not read the gap log at %s (%s)", self.path, e)
            return self._gaps
        self._gaps = [Gap.from_dict(g) for g in data if isinstance(g, dict)]
        return self._gaps

    def save(self) -> None:
        gaps = self.load()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([g.to_dict() for g in gaps[-MAX_GAPS:]], indent=2,
                           ensure_ascii=False),
                encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save the gap log: %s", e)

    def add(self, request: str, reason: str = "", domain: str = "") -> Gap:
        """Record a request. A near-identical earlier one is counted, not duplicated."""
        text = textutil.collapse_ws(request)
        if not text:
            raise ActionError("Cannot log an empty request as a missing capability.")
        for existing in self.load():
            if textutil.similarity(existing.request, text) >= SIMILARITY_MERGE:
                existing.attempts += 1
                existing.last_seen = time.time()
                if reason:
                    existing.reason = textutil.truncate(reason, 400)
                self.save()
                return existing
        gap = Gap(request=text[:400], reason=textutil.truncate(reason, 400),
                  domain=domain)
        self.load().append(gap)
        self.save()
        return gap

    def list(self, limit: int = 50, most_recent: bool = True) -> list[Gap]:
        gaps = self.load()
        ordered = sorted(gaps, key=lambda g: g.last_seen, reverse=most_recent)
        return ordered[:limit]

    def forget(self, request: str = "") -> int:
        gaps = self.load()
        if request:
            keep = [g for g in gaps
                    if textutil.similarity(g.request, request) < SIMILARITY_MERGE]
        else:
            keep = []
        removed = len(gaps) - len(keep)
        self._gaps = keep
        self.save()
        return removed

    def top(self, limit: int = 10) -> list[Gap]:
        return sorted(self.load(), key=lambda g: g.attempts, reverse=True)[:limit]

    def stats(self) -> dict:
        gaps = self.load()
        by_domain: dict[str, int] = {}
        for gap in gaps:
            key = gap.domain or "unclassified"
            by_domain[key] = by_domain.get(key, 0) + gap.attempts
        return {
            "requests": len(gaps),
            "total_attempts": sum(g.attempts for g in gaps),
            "by_domain": dict(sorted(by_domain.items(), key=lambda kv: -kv[1])),
            "path": str(self.path),
        }

    def close(self, request: str) -> bool:
        """Mark a request as now supported, and drop it from the list."""
        gaps = self.load()
        keep = [g for g in gaps
                if textutil.similarity(g.request, request) < SIMILARITY_MERGE]
        closed = len(keep) != len(gaps)
        self._gaps = keep
        self.save()
        return closed


_default: GapLog | None = None


def get_log(path: str | Path | None = None) -> GapLog:
    global _default
    if _default is None:
        if path is None:
            from ..config import active

            path = active().storage.home / "gaps.json"
        _default = GapLog(path)
    return _default


def set_default(store: GapLog | None) -> None:
    global _default
    _default = store


def log_gap(request: str, reason: str = "", domain: str = "") -> Gap:
    return get_log().add(request, reason=reason, domain=domain)
