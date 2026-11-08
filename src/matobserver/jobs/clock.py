"""시각 도우미: ISO 8601 UTC(`2026-11-08T03:12:00Z`, api.md §1)와 주입 가능한 시계."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """aware datetime -> `YYYY-MM-DDTHH:MM:SSZ` (초 단위, 소수 없음)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    """`...Z` 또는 오프셋이 있는 ISO 8601 -> aware UTC datetime. 오프셋이 없으면 UTC로 본다."""
    t = text.strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
