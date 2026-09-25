"""Append-only audit trail.

Every decision (allow *and* every kind of rejection) is recorded with an
event id, timestamp, caller (key_id only -- never the raw secret), route,
requested/resolved version, reason, and quota before/after values.

A redaction guard asserts that known plaintext secrets can never appear in a
serialized entry.
"""
from __future__ import annotations

import json
import logging
import threading

from gateway.models import AuditEntry

_RESERVED = ("api_key", "secret", "password", "token", "authorization")


def _redact(detail: dict) -> dict:
    clean = {}
    for k, v in detail.items():
        if any(w in k.lower() for w in _RESERVED):
            clean[k] = "***REDACTED***"
        else:
            clean[k] = v
    return clean


class AuditLog:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()
        self._logger = logger or logging.getLogger("gateway.audit")

    def append(self, entry: AuditEntry) -> AuditEntry:
        object.__setattr__(entry, "detail", _redact(dict(entry.detail)))
        with self._lock:
            self._entries.append(entry)
        self._logger.info(
            "audit event_id=%s decision=%s reason=%s key_id=%s route=%s "
            "requested_version=%s resolved_version=%s quota=%s->%s detail=%s",
            entry.event_id, entry.decision, entry.reason, entry.key_id,
            entry.route, entry.requested_version, entry.resolved_version,
            entry.quota_before, entry.quota_after, json.dumps(entry.detail),
        )
        return entry

    def entries(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)

    def assert_no_secret(self, secret: str) -> None:
        """Test helper: ensure a plaintext secret never reached the log."""
        with self._lock:
            blob = json.dumps([e.__dict__ for e in self._entries], default=str)
        assert secret not in blob, "plaintext secret leaked into audit log"
