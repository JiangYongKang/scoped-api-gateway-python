"""Per-route shared capacity ceiling, shared by *all* callers.

This is the backend-protection backstop: independent of any single
caller's quota, the total number of units admitted on a route within one
window can never exceed the route's shared capacity.

* ``try_acquire`` is atomic under a lock and all-or-nothing per request,
  so concurrent callers (different credentials on the same route) can
  never collectively overshoot the ceiling, and rejected requests hold
  nothing.
* ``rollback`` releases units when business handling fails after
  admission, so failed work never leaves dirty shared accounting.
* ``peek`` is read-only for usage queries.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from gateway.models import QuotaState


@dataclass(frozen=True)
class CapacityResult:
    allowed: bool
    used: int
    limit: int
    reset_at: datetime
    retry_after: int  # seconds until the window resets; 0 when allowed
    amount: int = 1


class SharedCapacityManager:
    def __init__(self, limits: dict[str, int],
                 window_seconds: int = 3600) -> None:
        self._limits = dict(limits)
        self._window = timedelta(seconds=window_seconds)
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._state: dict[str, QuotaState] = {}

    def limit_for(self, route: str) -> int:
        return self._limits.get(route, 0)

    def _current(self, route: str, now: datetime) -> QuotaState:
        st = self._state.get(route)
        if st is None or now >= st.window_start + self._window:
            st = QuotaState(window_start=now, used=0)
            self._state[route] = st
        return st

    def try_acquire(self, route: str, now: datetime,
                    amount: int = 1) -> CapacityResult:
        """Reserve ``amount`` shared units atomically, or nothing."""
        if amount < 1:
            raise ValueError("amount must be >= 1")
        limit = self.limit_for(route)
        with self._lock:
            st = self._current(route, now)
            reset_at = st.window_start + self._window
            retry_after = max(1, int((reset_at - now).total_seconds()))
            if st.used + amount > limit:
                return CapacityResult(False, st.used, limit, reset_at,
                                      retry_after, amount)
            st.used += amount
            return CapacityResult(True, st.used, limit, reset_at, 0, amount)

    def rollback(self, route: str, amount: int = 1) -> None:
        """Release shared units after a post-admission failure."""
        with self._lock:
            st = self._state.get(route)
            if st is not None and st.used > 0:
                st.used = max(0, st.used - amount)

    def peek(self, route: str, now: datetime) -> CapacityResult:
        """Read-only water-level view: never mutates any state."""
        limit = self.limit_for(route)
        with self._lock:
            st = self._state.get(route)
            if st is None or now >= st.window_start + self._window:
                return CapacityResult(True, 0, limit, now + self._window, 0, 0)
            reset_at = st.window_start + self._window
            retry_after = max(1, int((reset_at - now).total_seconds()))
            return CapacityResult(st.used < limit, st.used, limit, reset_at,
                                  retry_after, 0)
