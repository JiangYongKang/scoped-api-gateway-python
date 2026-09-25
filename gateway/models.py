"""Domain types shared across the admission layer."""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class VersionStatus(str, enum.Enum):
    SUPPORTED = "supported"
    DEPRECATED = "deprecated"
    RETIRED = "retired"
    UNKNOWN = "unknown"


class CredentialState(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class RejectReason(str, enum.Enum):
    VERSION_UNKNOWN = "version_unknown"
    VERSION_RETIRED = "version_retired"
    MISSING_CREDENTIAL = "missing_credential"
    CREDENTIAL_INVALID = "credential_invalid"
    CREDENTIAL_EXPIRED = "credential_expired"
    CREDENTIAL_REVOKED = "credential_revoked"
    FORBIDDEN = "forbidden"
    QUOTA_EXCEEDED = "quota_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class VersionInfo:
    version: str
    status: VersionStatus
    deprecated: bool = False
    sunset: datetime | None = None
    retirement: datetime | None = None
    successor: str | None = None


@dataclass(frozen=True)
class Credential:
    key_id: str
    secret_hash: str
    state: CredentialState
    valid_from: datetime
    valid_until: datetime
    scopes: frozenset[str] = frozenset()
    quota_limits: dict[str, int] = field(default_factory=dict)


@dataclass
class QuotaState:
    window_start: datetime
    used: int = 0


@dataclass
class IdempotencyRecord:
    key: str
    key_id: str
    route: str
    request_hash: str
    status_code: int
    body: Any
    created_at: datetime


@dataclass(frozen=True)
class AuditEntry:
    event_id: str
    ts: datetime
    key_id: str | None
    route: str
    requested_version: str | None
    resolved_version: str | None
    decision: str
    reason: str | None
    quota_before: int | None
    quota_after: int | None
    detail: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new(**kw: Any) -> "AuditEntry":
        return AuditEntry(event_id=uuid.uuid4().hex, ts=kw.pop("ts"), **kw)


@dataclass(frozen=True)
class AdmitResult:
    allowed: bool
    reason: RejectReason | None = None
    version: VersionInfo | None = None
    credential: Credential | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
