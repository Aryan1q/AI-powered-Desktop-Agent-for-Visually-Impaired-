"""Retry with exponential backoff and jitter, plus a deadline."""
from __future__ import annotations

import random
import time
from typing import Callable, Iterable, Type


def backoff_delays(base: float = 1.0, factor: float = 2.0,
                   max_delay: float = 30.0, count: int = 4,
                   jitter: float = 0.25, seed: int | None = None) -> list[float]:
    rng = random.Random(seed)
    out = []
    for i in range(max(0, count)):
        delay = min(max_delay, base * (factor ** i))
        if jitter:
            delay *= 1 + rng.uniform(-jitter, jitter)
        out.append(round(max(0.0, delay), 3))
    return out


def call(fn: Callable, *args,
         exceptions: Iterable[Type[BaseException]] = (Exception,),
         delays: list[float] | None = None,
         on_retry: Callable[[int, BaseException, float], None] | None = None,
         **kwargs):
    """Run `fn` until it succeeds or the delay list runs out."""
    waits = delays if delays is not None else [0.0]
    last: BaseException | None = None
    for attempt in range(len(waits) + 1):
        try:
            return fn(*args, **kwargs)
        except tuple(exceptions) as e:
            last = e
            if attempt >= len(waits):
                break
            wait = waits[attempt]
            if on_retry:
                on_retry(attempt, e, wait)
            if wait:
                time.sleep(wait)
    assert last is not None
    raise last


def until(predicate: Callable[[], bool], timeout: float = 10.0,
          interval: float = 0.25) -> bool:
    """Poll `predicate` until true or the deadline. Returns success."""
    end = time.time() + timeout
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if time.time() >= end:
            return False
        time.sleep(interval)


class Deadline:
    """A monotonic budget shared across several operations."""

    def __init__(self, seconds: float):
        self.seconds = max(0.0, float(seconds))
        self.started = time.monotonic()

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    @property
    def expired(self) -> bool:
        return self.remaining <= 0

    def check(self, what: str = "operation") -> None:
        from ..errors import BudgetExceeded

        if self.expired:
            raise BudgetExceeded(
                f"Deadline of {self.seconds:.1f}s exceeded during {what}."
            )
