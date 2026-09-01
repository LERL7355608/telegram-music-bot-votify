from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CachedTrack:
    track: dict[str, Any]
    expires_at: datetime


class TrackCache:
    def __init__(self, *, ttl_minutes: int = 30, max_items: int = 500):
        self.ttl = timedelta(minutes=ttl_minutes)
        self.max_items = max_items
        self._items: dict[str, CachedTrack] = {}

    def add(self, track: dict[str, Any]) -> str:
        return self.add_item(track)

    def get(self, ref: str) -> dict[str, Any] | None:
        return self.get_item(ref)

    def add_item(self, value: dict[str, Any]) -> str:
        self.cleanup()
        while len(self._items) >= self.max_items:
            oldest_key = min(self._items, key=lambda key: self._items[key].expires_at)
            self._items.pop(oldest_key, None)

        ref = secrets.token_urlsafe(8)
        self._items[ref] = CachedTrack(track=dict(value), expires_at=_now() + self.ttl)
        return ref

    def get_item(self, ref: str) -> dict[str, Any] | None:
        item = self._items.get(ref)
        if item is None:
            return None
        if item.expires_at <= _now():
            self._items.pop(ref, None)
            return None
        return dict(item.track)

    def cleanup(self) -> None:
        now = _now()
        expired = [ref for ref, item in self._items.items() if item.expires_at <= now]
        for ref in expired:
            self._items.pop(ref, None)
