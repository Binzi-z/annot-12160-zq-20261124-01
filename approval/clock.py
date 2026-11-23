"""Injectable clocks.

All persisted timestamps are UTC.  Business code never calls ``datetime.now``
directly; it uses a :class:`Clock` so tests and demos can move time
deterministically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current instant as an aware UTC datetime."""


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_timestamp(value: datetime | str) -> str:
    """Canonical, sortable UTC timestamp: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""

    return _as_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def to_utc(value: datetime | str) -> datetime:
    return _as_utc(value)


def parse_timestamp(value: str) -> datetime:
    return _as_utc(value)


class SystemClock:
    """Wall clock used by the CLI unless ``--now`` is supplied."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime | str = "2026-01-01T00:00:00Z") -> None:
        self._now = _as_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime | str) -> datetime:
        self._now = _as_utc(value)
        return self._now

    def advance(self, seconds: float = 0, *, minutes: float = 0, hours: float = 0) -> datetime:
        self._now = self._now + timedelta(seconds=seconds + minutes * 60 + hours * 3600)
        return self._now
