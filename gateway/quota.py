"""Per-credential, per-route fixed-window quota accounting.

Concurrency correctness:

* ``try_acquire`` is atomic under a lock, so concurrent requests can never
  both observe a free slot (no over-admission) and rejected attempts mutate
  nothing (no over-counting).
* Only admitted requests consume quota. Requests rejected by an *earlier*
  pipeline stage (auth/scope/version) never reach the quota stage, and
  rejected acquire attempts leave the counter untouched.
* If an admitted request ultimately fails before the side effect happens,
  the route calls ``rollback`` so no quota is leaked.
* Replays of completed idempotent requests never call ``try_acquire``.
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


class QuotaManager:
    def __init__(self, window_seconds: int = 3600) -> None:
        self._window = timedelta(seconds=window_seconds)
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], QuotaState] = {}

    def try_acquire(self, key_id: str, route: str, limit: int,
                    now: datetime) -> AcquireResult:
        key = (key_id, route)
        with self._lock:
            st = self._state.get(key)
            if st is None or now >= st.window_start + self._window:
                st = QuotaState(window_start=now, used=0)
                self._state[key] = st
            reset_at = st.window_start + self._window
            retry_after = max(1, int((reset_at - now).total_seconds()))
            if st.used >= limit:
                return AcquireResult(False, st.used, limit, reset_at, retry_after)
            st.used += 1
            return AcquireResult(True, st.used, limit, reset_at, 0)

    def rollback(self, key_id: str, route: str, now: datetime | None = None) -> None:
        """Release a previously acquired slot (failed business handling)."""
        key = (key_id, route)
        with self._lock:
            st = self._state.get(key)
            if st is not None and st.used > 0:
                st.used -= 1
