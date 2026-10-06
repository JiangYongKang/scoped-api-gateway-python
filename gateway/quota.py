"""Per-credential, per-route fixed-window quota accounting.

Concurrency correctness:

* ``try_acquire`` is atomic under a lock, so concurrent requests can never
  both observe a free slot (no over-admission) and rejected attempts mutate
  nothing (no over-counting). Batch callers pass ``amount``; the whole
  amount is admitted or nothing is -- there is no partial acquisition.
* Only admitted requests consume quota. Requests rejected by an *earlier*
  pipeline stage (auth/scope/version) never reach the quota stage, and
  rejected acquire attempts leave the counter untouched.
* If an admitted request ultimately fails before the side effect happens,
  the route calls ``rollback`` with the same amount so no quota is leaked.
* Replays of completed idempotent requests never call ``try_acquire``.
* ``peek`` reports the current water level without mutating anything, so
  usage introspection can never consume or hold quota.

The same class also backs the per-route *shared* capacity budget: it is
just a second instance keyed by a reserved caller id, so the shared limit
gets the same atomic fixed-window semantics under concurrency.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from gateway.models import QuotaState

# Reserved caller id for the per-route shared capacity bucket. It can never
# collide with a real credential key_id (seeded ids use an "acct-" prefix).
SHARED_KEY_ID = "__shared__"


@dataclass(frozen=True)
class AcquireResult:
    allowed: bool
    used: int
    limit: int
    reset_at: datetime
    retry_after: int  # seconds until the window resets; 0 when allowed
    amount: int = 1   # units this call admitted (or asked for, if rejected)


class QuotaManager:
    def __init__(self, window_seconds: int = 3600) -> None:
        self._window = timedelta(seconds=window_seconds)
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], QuotaState] = {}

    def try_acquire(self, key_id: str, route: str, limit: int,
                    now: datetime, amount: int = 1) -> AcquireResult:
        """Atomically admit ``amount`` units, or nothing at all.

        A request whose remaining budget cannot cover the full amount is
        rejected *without* touching the counter: no partial admission and
        no charge-then-refund intermediate state.
        """
        if amount < 1:
            raise ValueError("amount must be >= 1")
        key = (key_id, route)
        with self._lock:
            st = self._state.get(key)
            if st is None or now >= st.window_start + self._window:
                st = QuotaState(window_start=now, used=0)
                self._state[key] = st
            reset_at = st.window_start + self._window
            retry_after = max(1, int((reset_at - now).total_seconds()))
            if st.used + amount > limit:
                return AcquireResult(False, st.used, limit, reset_at,
                                     retry_after, amount)
            st.used += amount
            return AcquireResult(True, st.used, limit, reset_at, 0, amount)

    def rollback(self, key_id: str, route: str, amount: int = 1,
                 now: datetime | None = None) -> None:
        """Release previously acquired units (failed business handling)."""
        key = (key_id, route)
        with self._lock:
            st = self._state.get(key)
            if st is not None:
                st.used = max(0, st.used - amount)

    def peek(self, key_id: str, route: str, limit: int,
             now: datetime) -> AcquireResult:
        """Read the current water level without mutating any state.

        An expired window is reported as a fresh one (used=0) but the
        stored state is left alone, so querying usage never consumes,
        resets, or extends quota.
        """
        key = (key_id, route)
        with self._lock:
            st = self._state.get(key)
            if st is None or now >= st.window_start + self._window:
                return AcquireResult(True, 0, limit, now + self._window, 0, 0)
            reset_at = st.window_start + self._window
            retry_after = max(1, int((reset_at - now).total_seconds()))
            return AcquireResult(st.used < limit, st.used, limit, reset_at,
                                 retry_after, 0)
