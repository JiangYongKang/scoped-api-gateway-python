"""Sequential admission pipeline with a decision at each stage.

Order is deliberate and every outcome (allow or reject) is audited:

1. Version negotiation (400 unknown / 410 retired -- distinct from auth).
2. Credential presence and validity (401 invalid/expired/revoked).
3. Route scope (403 insufficient_permissions).
4. Idempotency judgement for writes (replay/conflict short-circuit *before*
   quota, so repeats never consume or temporarily hold quota).
5. Per-credential quota (429 quota_exceeded) -- first writers only.
6. Shared per-route capacity (429 capacity_exceeded) -- all callers combined;
   if this stage rejects, the quota units held by stage 5 are rolled back.

Rejected requests at stages 1-3 never touch quota, so they cannot consume it.
The usage-query pipeline stops after stage 2: it is read-only and consumes
neither quota nor capacity.
"""
from __future__ import annotations

from gateway.errors import GatewayError
from gateway.models import (AdmitResult, AuditEntry, RejectReason,
                            VersionStatus)
from gateway.store import DEFAULT_LIMITS
from gateway.versioning import DEFAULT_VERSION, VERSIONS, resolve_version


def _audit(services, *, key_id, route, requested, resolved, decision,
           reason, before=None, after=None, detail=None) -> None:
    services["audit"].append(AuditEntry.new(
        ts=services["clock"].now(), key_id=key_id, route=route,
        requested_version=requested, resolved_version=resolved,
        decision=decision, reason=reason,
        quota_before=before, quota_after=after, detail=detail or {}))


def _reject(services, *, route, requested, resolved, key_id, reason: RejectReason,
            message: str, status: int, before=None, after=None,
            detail=None, headers=None) -> None:
    _audit(services, key_id=key_id, route=route, requested=requested,
           resolved=resolved, decision="reject", reason=reason.value,
           before=before, after=after, detail=detail)
    raise GatewayError(status, reason, message, extra=detail, headers=headers)


def _version(services, route, requested_version):
    requested = requested_version or DEFAULT_VERSION
    try:
        version = resolve_version(requested_version)
    except KeyError as exc:
        if exc.args[0] is VersionStatus.RETIRED:
            info = VERSIONS[requested]
            _reject(services, route=route, requested=requested, resolved=requested,
                    key_id=None, reason=RejectReason.VERSION_RETIRED,
                    message=f"API version {requested} has been retired.",
                    status=410,
                    detail={"retirement": info.retirement.date().isoformat(),
                            "successor": info.successor,
                            "supported": ["v2", "v3"],
                            "default_version": DEFAULT_VERSION})
        _reject(services, route=route, requested=requested, resolved=None,
                key_id=None, reason=RejectReason.VERSION_UNKNOWN,
                message=f"Unknown API version: {requested!r}.",
                status=400,
                detail={"supported": ["v2", "v3"],
                        "deprecated": ["v1"],
                        "default_version": DEFAULT_VERSION})
    return requested, version


def _credential(services, route, requested, version, api_key):
    if not api_key:
        _reject(services, route=route, requested=requested,
                resolved=version.version, key_id=None,
                reason=RejectReason.MISSING_CREDENTIAL,
                message="Missing API credential. Provide X-Api-Key.",
                status=401)
    try:
        return services["registry"].authenticate(
            api_key, services["clock"].now())
    except GatewayError as exc:
        _reject(services, route=route, requested=requested,
                resolved=version.version, key_id=exc.extra.get("key_id"),
                reason=exc.code, message=exc.message,
                status=exc.status_code, detail=exc.extra)


def _scope(services, route, requested, version, credential):
    if not services["registry"].has_scope(credential, route):
        _reject(services, route=route, requested=requested,
                resolved=version.version, key_id=credential.key_id,
                reason=RejectReason.FORBIDDEN,
                message="Credential lacks the scope required for this route.",
                status=403,
                detail={"key_id": credential.key_id, "required_scope": route})


def _quota(services, route, requested, version, credential):
    limit = credential.quota_limits.get(
        route, DEFAULT_LIMITS.get(route, 0))
    res = services["quota"].try_acquire(
        credential.key_id, route, limit, services["clock"].now())
    if not res.allowed:
        _reject(services, route=route, requested=requested,
                resolved=version.version, key_id=credential.key_id,
                reason=RejectReason.QUOTA_EXCEEDED,
                message="Per-credential route quota exceeded.",
                status=429, before=res.used, after=res.used,
                detail={"key_id": credential.key_id, "route": route,
                        "used": res.used, "limit": res.limit,
                        "window_seconds": services["quota"].window_seconds,
                        "retry_after_seconds": res.retry_after,
                        "reset_at": res.reset_at.isoformat()},
                headers={"Retry-After": str(res.retry_after)})
    _audit(services, key_id=credential.key_id, route=route,
           requested=requested, resolved=version.version,
           decision="allow", reason=None,
           before=res.used - 1, after=res.used,
           detail={"key_id": credential.key_id, "limit": res.limit,
                   "reset_at": res.reset_at.isoformat()})
    return res


def _capacity(services, route, requested, version, credential, amount=1):
    """Shared per-route ceiling across all callers (backend backstop)."""
    res = services["capacity"].try_acquire(
        route, services["clock"].now(), amount=amount)
    if not res.allowed:
        _reject(services, route=route, requested=requested,
                resolved=version.version, key_id=credential.key_id,
                reason=RejectReason.CAPACITY_EXCEEDED,
                message="Shared route capacity exhausted; the route is full "
                        "for all callers in this window.",
                status=429, before=res.used, after=res.used,
                detail={"key_id": credential.key_id, "route": route,
                        "shared_used": res.used, "shared_limit": res.limit,
                        "window_seconds": services["capacity"].window_seconds,
                        "retry_after_seconds": res.retry_after,
                        "reset_at": res.reset_at.isoformat()},
                headers={"Retry-After": str(res.retry_after)})
    return res


def admit(*, requested_version: str | None, api_key: str | None,
          route: str, services: dict) -> AdmitResult:
    """Full pipeline for read routes (version -> auth -> scope -> quota ->
    shared capacity)."""
    requested, version = _version(services, route, requested_version)
    credential = _credential(services, route, requested, version, api_key)
    _scope(services, route, requested, version, credential)
    res = _quota(services, route, requested, version, credential)
    try:
        cap = _capacity(services, route, requested, version, credential)
    except GatewayError:
        # Never leak per-credential quota when the shared ceiling rejects.
        services["quota"].rollback(credential.key_id, route)
        raise
    return AdmitResult(allowed=True, version=version, credential=credential,
                       detail={"used": res.used, "limit": res.limit,
                               "reset_at": res.reset_at.isoformat(),
                               "shared_used": cap.used,
                               "shared_limit": cap.limit})


def admit_usage(*, requested_version: str | None, api_key: str | None,
                route: str, services: dict) -> AdmitResult:
    """Usage-query pipeline: version + credential only.

    Read-only self-service: no scope requirement, and it consumes neither
    quota nor shared capacity nor mutates any window state.
    """
    requested, version = _version(services, route, requested_version)
    credential = _credential(services, route, requested, version, api_key)
    _audit(services, key_id=credential.key_id, route=route,
           requested=requested, resolved=version.version,
           decision="allow", reason=None,
           detail={"key_id": credential.key_id, "usage_query": True})
    return AdmitResult(allowed=True, version=version, credential=credential)


def admit_pre_quota(*, requested_version: str | None, api_key: str | None,
                    route: str, services: dict) -> AdmitResult:
    """Stages 1-3 for write routes; quota is acquired only after a write has
    claimed its idempotency key, so replays/conflicts never touch quota."""
    requested, version = _version(services, route, requested_version)
    credential = _credential(services, route, requested, version, api_key)
    _scope(services, route, requested, version, credential)
    return AdmitResult(allowed=True, version=version, credential=credential)
