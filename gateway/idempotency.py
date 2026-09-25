"""Idempotency key storage with concurrency-safe first-writer semantics.

* The first request for a key reserves it ("stored"); concurrent duplicates
  wait for the first result and then replay it, so under concurrency exactly
  one side effect happens.
* A completed key always replays the original status code and body.
* The same key with a different payload hash or a different caller/route is
  a hard 409 conflict ("idempotency_conflict").
* If the first writer fails before completion, the reservation is released so
  the caller can safely retry with the same key.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from gateway.models import IdempotencyRecord, utcnow

STORED = "stored"
REPLAY = "replay"
CONFLICT = "conflict"
INFLIGHT = "inflight"


@dataclass
class _Entry:
    key_id: str
    route: str
    request_hash: str
    event: threading.Event = field(default_factory=threading.Event)
    record: IdempotencyRecord | None = None


class IdempotencyStore:
    def __init__(self, inflight_timeout: float = 5.0) -> None:
        self._table_lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._inflight_timeout = inflight_timeout

    def begin(self, key: str, key_id: str, route: str,
              request_hash: str) -> tuple[str, IdempotencyRecord | None]:
        with self._table_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(key_id=key_id, route=route, request_hash=request_hash)
                self._entries[key] = entry
                return STORED, None
            if (entry.key_id, entry.route, entry.request_hash) != (
                    key_id, route, request_hash):
                return CONFLICT, entry.record

        # Same request repeated concurrently: wait for the first writer.
        if entry.event.wait(timeout=self._inflight_timeout):
            if entry.record is not None:
                return REPLAY, entry.record
            # First writer explicitly failed; reservation was released.
            with self._table_lock:
                self._entries.pop(key, None)
            return self.begin(key, key_id, route, request_hash)
        return INFLIGHT, None

    def complete(self, key: str, status_code: int, body: Any) -> IdempotencyRecord:
        with self._table_lock:
            entry = self._entries[key]
            record = IdempotencyRecord(
                key=key, key_id=entry.key_id, route=entry.route,
                request_hash=entry.request_hash, status_code=status_code,
                body=body, created_at=utcnow())
            entry.record = record
            entry.event.set()
            return record

    def fail(self, key: str) -> None:
        """Release a reservation whose first writer could not finish."""
        with self._table_lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            entry.event.set()

    def purge_older_than(self, age: timedelta) -> int:
        cutoff = utcnow() - age
        with self._table_lock:
            stale = [k for k, e in self._entries.items()
                     if e.record is not None and e.record.created_at < cutoff]
            for k in stale:
                del self._entries[k]
        return len(stale)
