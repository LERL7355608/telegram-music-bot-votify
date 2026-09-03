from __future__ import annotations

import time
from collections import defaultdict, deque


class InMemoryRateLimiter:
    def __init__(self, max_events: int, window_seconds: int = 3600):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, cost: int = 1) -> bool:
        """Consume `cost` slots for this user. Returns False and consumes nothing if over budget."""
        cost = max(1, cost)
        now = time.time()
        events = self._events[user_id]

        while events and now - events[0] >= self.window_seconds:
            events.popleft()

        if len(events) + cost > self.max_events:
            return False

        for _ in range(cost):
            events.append(now)
        return True

    def remaining(self, user_id: int) -> int:
        now = time.time()
        events = self._events[user_id]
        while events and now - events[0] >= self.window_seconds:
            events.popleft()
        return max(0, self.max_events - len(events))
