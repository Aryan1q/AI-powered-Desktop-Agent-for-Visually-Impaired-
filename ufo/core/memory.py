"""Long-term memory: what worked, what failed, and what the user prefers.

Two separate stores, deliberately:

  * **Facts** — durable preferences and knowledge ("the user's editor is
    Notepad", "downloads live in D:/Downloads"). Injected into every prompt.
  * **Episodes** — per-task outcomes with the actions that produced them.
    Retrieved by relevance when a similar task comes up, so the agent does not
    rediscover the same dead end twice.

Everything is a local JSON file. Nothing is sent anywhere.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..logging_setup import get_logger
from ..safety import secrets
from ..utils import text as textutil

log = get_logger(__name__)

MAX_EPISODES = 500
MAX_FACTS = 200


@dataclass
class Episode:
    task: str
    outcome: str = ""
    success: bool = False
    steps: int = 0
    duration: float = 0.0
    actions: list[str] = field(default_factory=list)
    lesson: str = ""
    at: float = field(default_factory=time.time)
    app: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Episode":
        return cls(
            task=str(data.get("task", ""))[:400],
            outcome=str(data.get("outcome", ""))[:200],
            success=bool(data.get("success", False)),
            steps=int(data.get("steps", 0)),
            duration=float(data.get("duration", 0.0)),
            actions=[str(a)[:200] for a in (data.get("actions") or [])][-30:],
            lesson=textutil.truncate(str(data.get("lesson", "")), 500),
            at=float(data.get("at", 0.0) or time.time()),
            app=str(data.get("app", ""))[:120],
        )


@dataclass
class Fact:
    key: str
    value: str
    source: str = ""
    at: float = field(default_factory=time.time)
    uses: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class Memory:
    """A tiny, file-backed store. Safe to open from several processes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._facts: dict[str, Fact] = {}
        self._episodes: list[Episode] = []
        self._loaded = False

    # ---- persistence ----
    def load(self) -> "Memory":
        if self._loaded:
            return self
        self._loaded = True
        if not self.path.exists():
            return self
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("could not read memory at %s (%s); starting fresh",
                        self.path, e)
            return self
        self._facts = {k: Fact(**v) for k, v in (data.get("facts") or {}).items()
                       if isinstance(v, dict)}
        self._episodes = [Episode.from_dict(e) for e in (data.get("episodes") or [])
                          if isinstance(e, dict)]
        return self

    def save(self) -> None:
        self.load()
        payload = {
            "version": 1,
            "updated": time.time(),
            "facts": {k: v.to_dict() for k, v in self._facts.items()},
            "episodes": [e.to_dict() for e in self._episodes[-MAX_EPISODES:]],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                           encoding="utf-8")
            tmp.replace(self.path)          # atomic: never leave a half-written file
        except OSError as e:
            log.warning("could not save memory: %s", e)

    # ---- facts ----
    def remember(self, key: str, value: str, source: str = "") -> None:
        """Store one fact. Secrets are redacted on the way in.

        Memory is a prompt-injection surface that lands in plaintext on disk,
        so a value that looks like a key or password is replaced rather than
        persisted. `SecretStore` is the place for credentials the agent must
        actually use.
        """
        key = textutil.collapse_ws(key).lower()
        if not key:
            return
        self.load()
        safe = secrets.redact(str(value))
        if safe != str(value):
            log.warning("Redacted a secret-looking value for memory key %r.", key)
        self._facts[key] = Fact(key=key, value=safe[:500], source=source[:200])
        self._trim_facts()
        self.save()

    def recall(self, key: str, default: str = "") -> str:
        self.load()
        fact = self._facts.get(textutil.collapse_ws(key).lower())
        if fact is None:
            return default
        fact.uses += 1
        self.save()
        return fact.value

    def forget(self, key: str) -> bool:
        self.load()
        removed = self._facts.pop(textutil.collapse_ws(key).lower(), None)
        if removed is not None:
            self.save()
        return removed is not None

    def facts(self) -> dict[str, str]:
        self.load()
        return {k: v.value for k, v in sorted(self._facts.items())}

    def _trim_facts(self) -> None:
        if len(self._facts) > MAX_FACTS:
            for key, _ in sorted(self._facts.items(),
                                 key=lambda kv: kv[1].at)[: len(self._facts) - MAX_FACTS]:
                self._facts.pop(key, None)

    # ---- episodes ----
    def record(self, episode: Episode) -> None:
        self.load()
        self._episodes.append(episode)
        if len(self._episodes) > MAX_EPISODES:
            self._episodes = self._episodes[-MAX_EPISODES:]
        if episode.lesson:
            key = f"lesson:{textutil.slugify(episode.task, 40)}"
            self._facts[key] = Fact(key=key, value=episode.lesson, source="episode")
        self.save()

    def episodes(self, limit: int = 50, success: bool | None = None) -> list[Episode]:
        self.load()
        items = [e for e in self._episodes if success is None or e.success is success]
        return items[-limit:][::-1]

    def similar(self, task: str, limit: int = 5) -> list[Episode]:
        """Episodes similar to a task, best match first."""
        self.load()
        scored = [
            (textutil.similarity(task, e.task), e)
            for e in self._episodes if e.task
        ]
        scored = [(s, e) for s, e in scored if s > 0.25]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [e for _s, e in scored[:limit]]

    def lessons_for(self, task: str, limit: int = 5) -> list[str]:
        return [e.lesson for e in self.similar(task, limit) if e.lesson]

    # ---- prompt context ----
    def brief(self, task: str = "", limit: int = 5,
              max_chars: int = 2000) -> str:
        """The memory slice injected into a prompt. Empty string when there is
        nothing useful, so it never wastes tokens.

        `max_chars` bounds the *whole* slice: 20 facts with 500-character values
        would otherwise push 10k characters of noise into every prompt.
        """
        self.load()
        lines: list[str] = []

        def room() -> int:
            return max_chars - sum(len(line) + 1 for line in lines)

        def add(line: str) -> None:
            if len(line) + 1 <= room():
                lines.append(line)

        facts = self.facts()
        if facts:
            add("WHAT I KNOW ABOUT THIS COMPUTER AND USER:")
            for k, v in list(facts.items())[:20]:
                add(f"- {textutil.truncate(k, 40)}: {textutil.truncate(str(v), 120)}")

        if task:
            relevant = self.similar(task, limit)
            if relevant:
                add("")
                add("SIMILAR TASKS I HAVE DONE BEFORE:")
                for e in relevant:
                    mark = "succeeded" if e.success else "failed"
                    lesson = f" — lesson: {textutil.truncate(e.lesson, 120)}" if e.lesson else ""
                    add(f"- {mark}: {textutil.truncate(e.task, 100)}{lesson}")
        return "\n".join(lines)[:max_chars]

    def clear(self) -> None:
        self._facts.clear()
        self._episodes.clear()
        self.save()

    def stats(self) -> dict:
        self.load()
        successes = sum(1 for e in self._episodes if e.success)
        return {
            "facts": len(self._facts),
            "episodes": len(self._episodes),
            "success_rate": round(successes / len(self._episodes), 3) if self._episodes else None,
            "total_actions": sum(e.steps for e in self._episodes),
            "path": str(self.path),
        }


_default: Memory | None = None


def get_memory(path: str | Path | None = None) -> Memory:
    """Process-wide memory store, bound to the configured home on first use.

    Named `get_memory`, not `memory`, so `from ..core import memory` still
    yields this module rather than shadowing it with a callable.
    """
    global _default
    if _default is None:
        if path is None:
            from ..config import active

            path = active().storage.memory_path
        _default = Memory(path)
    return _default


def set_default(store: Memory | None) -> None:
    global _default
    _default = store


def from_tasks(tasks: Iterable[Any], limit: int = 20) -> list[Episode]:
    """Build episodes from a run history (used when persisting a session)."""
    out: list[Episode] = []
    for t in list(tasks)[-limit:]:
        out.append(Episode(
            task=getattr(t, "goal", ""),
            outcome=getattr(getattr(t, "result", None), "outcome", ""),
            success=bool(getattr(getattr(t, "result", None), "status", None)
                         and getattr(t.result.status, "successful", False)),
            steps=int(getattr(getattr(t, "result", None), "actions", 0) or 0),
            duration=float(getattr(getattr(t, "result", None), "duration", 0.0) or 0.0),
        ))
    return out
