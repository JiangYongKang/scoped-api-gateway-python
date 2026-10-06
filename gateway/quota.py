"""Per-credential, per-route fixed-window quota accounting.

Concurrency correctness:

* ``try_acquire`` is atomic under a lock, so concurrent requests can never
  both observe a free slot (no over-admission) and rejected attempts mutate
  nothing (no over-counting).
* Batch acquisitions are all-or-nothing: ``try_acquire(amount=n)`` either
  reserves all ``n`` units or none -- a request is never admitted with a
  partially covered batch, and a rejected batch holds no quota at all.
* Only admitted requests consume quota. Requests rejected by an *earlier*
  pipeline stage (auth/scope/version) never reach the quota stage, and
  rejected acquire attempts leave the counter untouched.
* If an admitted request ultimately fails before the side effect happens,
  the route calls ``rollback`` with the same amount so no quota is leaked.
* Replays of completed idempotent requests never call ``try_acquire``.
* ``peek`` is read-only: usage queries never mutate window state.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from gateway.models import QuotaState


@dataclass(frozen=True)
class AcquireResult:
    allowed: bool
    used: int
    limit: int
    reset_at: datetime
    retry_after: int  # seconds until the window resets; 0 when allowed
    amount: int = 1   # units requested (and reserved, when allowed)


class QuotaManager:
    def __init__(self, window_seconds: int = 3600) -> None:
        self._window = timedelta(seconds=window_seconds)
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], QuotaState] = {}

    def _current(self, key: tuple[str, str], now: datetime) -> QuotaState:
        """Window state for ``key`` at ``now`` (caller holds the lock)."""
        st = self._state.get(key)
        if st is None or now >= st.window_start + self._window:
            st = QuotaState(window_start=now, used=0)
            self._state[key] = st
        return st

    def try_acquire(self, key_id: str, route: str, limit: int,
                    now: datetime, amount: int = 1) -> AcquireResult:
        """Reserve ``amount`` units atomically, or nothing at all."""
        if amount < 1:
            raise ValueError("amount must be >= 1")
        key = (key_id, route)
        with self._lock:
            st = self._current(key, now)
            reset_at = st.window_start + self._window
            retry_after = max(1, int((reset_at - now).total_seconds()))
            if st.used + amount > limit:
                # Whole batch refused: counter untouched, nothing held.
                return AcquireResult(False, st.used, limit, reset_at,
                                     retry_after, amount)
            st.used += amount
            return AcquireResult(True, st.used, limit, reset_at, 0, amount)

    def rollback(self, key_id: str, route: str, now: datetime | None = None,
                 amount: int = 1) -> None:
        """Release previously acquired units (failed business handling)."""
        key = (key_id, route)
        with self._lock:
            st = self._state.get(key)
            if st is not None and st.used > 0:
                st.used = max(0, st.used - amount)

    def peek(self, key_id: str, route: str, limit: int,
             now: datetime) -> AcquireResult:
        """Read-only view of the bucket: never mutates any state."""
        key = (key_id, route)
        with self._lock:
            st = self._state.get(key)
            if st is None or now >= st.window_start + self._window:
                # Window has (or would have) reset; report a fresh window
                # without persisting anything.
                return AcquireResult(True, 0, limit, now + self._window, 0, 0)
            reset_at = st.window_start + self._window
            retry_after = max(1, int((reset_at - now).total_seconds()))
            return AcquireResult(st.used < limit, st.used, limit, reset_at,
                                 retry_after, 0)
