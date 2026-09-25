"""Service composition.

A single ``services`` dict is built per app (or per test). Tests may inject a
controllable clock to exercise expiry, rotation and quota-window boundaries.
"""
from __future__ import annotations

from datetime import datetime

from gateway.audit import AuditLog
from gateway.clock import Clock, SystemClock
from gateway.idempotency import IdempotencyStore
from gateway.quota import QuotaManager
from gateway.store import build_registry, build_widgets


class FixedClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta) -> None:
        from datetime import timedelta
        self._now += timedelta(**delta)


def build_services(clock: Clock | None = None,
                   quota_window_seconds: int = 3600) -> dict:
    clock = clock or SystemClock()
    now = clock.now()
    return {
        "clock": clock,
        "registry": build_registry(now),
        "quota": QuotaManager(window_seconds=quota_window_seconds),
        "idempotency": IdempotencyStore(),
        "audit": AuditLog(),
        "widgets": build_widgets(),
    }
