"""Business routes shared, unchanged in semantics, across all versions.

Read route order: version -> credential -> scope -> quota -> shared
capacity (see ``admit``).

Write route order (single create and batch create alike):

    version -> credential -> scope
      -> idempotency begin (replay / conflict return HERE, never touching quota)
      -> per-credential quota acquire (amount = records to write)
      -> shared route capacity acquire (same amount; on rejection the quota
         units are rolled back -- a full route never costs the caller budget)
        -> business side effect (exactly once per key)
      -> idempotency complete
        -> failure before completion: quota + capacity rollback + key release

Batch writes are all-or-nothing: if the remaining budget cannot cover the
whole batch the request is rejected before anything is held, and a business
failure mid-batch rolls back every unit acquired. Replays therefore consume
no quota and return the first response verbatim; conflicts likewise leave
quota and widget state untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.admission import _audit, admit, admit_introspect, admit_pre_quota
from gateway.errors import GatewayError, error_body
from gateway.idempotency import CONFLICT, INFLIGHT, REPLAY
from gateway.models import RejectReason
from gateway.quota import SHARED_KEY_ID
from gateway.store import (
    DEFAULT_LIMITS,
    ROUTE_CAPACITY,
    ROUTE_WIDGET_DETAIL,
    ROUTE_WIDGETS_CREATE,
    ROUTE_WIDGETS_LIST,
)
from gateway.versioning import DEFAULT_VERSION, deprecation_headers

router = APIRouter()

# Idempotency/audit label for the batch endpoint. Quota, capacity and scope
# are charged against ROUTE_WIDGETS_CREATE itself: a batch is just a bulk
# create drawn from the same budget.
ROUTE_WIDGETS_BATCH = "widgets:batch_create"

# Audit label for the self-service usage introspection endpoint.
ROUTE_USAGE = "usage:inspect"

# Upper bound on records per batch request.
MAX_BATCH_SIZE = 100


def _services(request: Request) -> dict:
    return request.app.state.services


def _rate_headers(result, used: int | None = None,
                  limit: int | None = None) -> dict[str, str]:
    headers = {"X-Api-Version": result.version.version}
    used = used if used is not None else result.detail.get("used")
    limit = limit if limit is not None else result.detail.get("limit")
    if used is not None and limit is not None:
        headers["X-RateLimit-Limit"] = str(limit)
        headers["X-RateLimit-Remaining"] = str(max(0, limit - used))
    return headers


def _deprecation(result, headers: dict[str, str]) -> dict[str, str]:
    headers.update(deprecation_headers(result.version))
    return headers


def _envelope(version: str, data, replay: bool = False) -> dict:
    env = {"api_version": version, "data": data}
    if replay:
        env["idempotent_replay"] = True
    return env


def _write_budget(services, result, cred, idem_key: str,
                  requested_version: str | None, amount: int,
                  audit_route: str):
    """Acquire per-credential quota then shared route capacity for a write.

    Atomicity guarantees:

    * quota admits the full ``amount`` or nothing (no partial batches);
    * if the shared capacity ceiling trips, the quota units just acquired
      are rolled back, so a full route never drains the caller's budget;
    * any rejection releases the idempotency reservation, so the caller can
      retry the same key later and nothing is left held.

    Returns ``(quota_result, capacity_result)`` on success.
    """
    route = ROUTE_WIDGETS_CREATE
    now = services["clock"].now()
    requested = requested_version or DEFAULT_VERSION
    q = services["quota"].try_acquire(
        cred.key_id, route, cred.quota_limits[route], now, amount)
    if not q.allowed:
        services["idempotency"].fail(idem_key)
        _audit(services, key_id=cred.key_id, route=audit_route,
               requested=requested, resolved=result.version.version,
               decision="reject", reason=RejectReason.QUOTA_EXCEEDED.value,
               before=q.used, after=q.used,
               detail={"key_id": cred.key_id, "idempotency_key": idem_key,
                       "amount": amount,
                       "retry_after_seconds": q.retry_after})
        raise GatewayError(
            429, RejectReason.QUOTA_EXCEEDED,
            "Per-credential route quota exceeded.",
            extra={"key_id": cred.key_id, "route": route,
                   "scope": "per_credential_quota",
                   "requested": amount,
                   "used": q.used, "limit": q.limit,
                   "retry_after_seconds": q.retry_after,
                   "reset_at": q.reset_at.isoformat()},
            headers={"Retry-After": str(q.retry_after)})
    cap = services["capacity"].try_acquire(
        SHARED_KEY_ID, route, ROUTE_CAPACITY[route], now, amount)
    if not cap.allowed:
        services["quota"].rollback(cred.key_id, route, amount)
        services["idempotency"].fail(idem_key)
        _audit(services, key_id=cred.key_id, route=audit_route,
               requested=requested, resolved=result.version.version,
               decision="reject", reason=RejectReason.CAPACITY_EXCEEDED.value,
               before=cap.used, after=cap.used,
               detail={"key_id": cred.key_id, "idempotency_key": idem_key,
                       "amount": amount,
                       "retry_after_seconds": cap.retry_after})
        raise GatewayError(
            429, RejectReason.CAPACITY_EXCEEDED,
            "Shared route capacity exhausted; the route is temporarily "
            "full for all callers.",
            extra={"key_id": cred.key_id, "route": route,
                   "scope": "shared_route_capacity",
                   "requested": amount,
                   "used": cap.used, "limit": cap.limit,
                   "retry_after_seconds": cap.retry_after,
                   "reset_at": cap.reset_at.isoformat()},
            headers={"Retry-After": str(cap.retry_after)})
    _audit(services, key_id=cred.key_id, route=audit_route,
           requested=requested, resolved=result.version.version,
           decision="allow", reason=None,
           before=q.used - amount, after=q.used,
           detail={"key_id": cred.key_id, "limit": q.limit,
                   "idempotency_key": idem_key, "amount": amount,
                   "capacity_used": cap.used, "capacity_limit": cap.limit})
    return q, cap


def _rollback_write_budget(services, cred, amount: int) -> None:
    """Return every unit a failed write held (quota + shared capacity)."""
    services["quota"].rollback(cred.key_id, ROUTE_WIDGETS_CREATE, amount)
    services["capacity"].rollback(SHARED_KEY_ID, ROUTE_WIDGETS_CREATE, amount)


@router.get("/widgets")
def list_widgets(request: Request):
    services = _services(request)
    result = admit(
        requested_version=request.headers.get("x-api-version"),
        api_key=request.headers.get("x-api-key"),
        route=ROUTE_WIDGETS_LIST, services=services)
    items = sorted(services["widgets"].values(), key=lambda w: w["id"])
    headers = _deprecation(result, _rate_headers(result))
    return JSONResponse(_envelope(result.version.version, {"widgets": items}),
                        headers=headers)


@router.get("/widgets/{widget_id}")
def get_widget(widget_id: str, request: Request):
    services = _services(request)
    result = admit(
        requested_version=request.headers.get("x-api-version"),
        api_key=request.headers.get("x-api-key"),
        route=ROUTE_WIDGET_DETAIL, services=services)
    widget = services["widgets"].get(widget_id)
    if widget is None:
        raise GatewayError(404, RejectReason.NOT_FOUND,
                           f"Widget {widget_id!r} not found.",
                           extra={"widget_id": widget_id})
    headers = _deprecation(result, _rate_headers(result))
    return JSONResponse(_envelope(result.version.version, {"widget": widget}),
                        headers=headers)


@router.get("/usage")
def get_usage(request: Request):
    """Self-service usage introspection for the authenticated caller.

    Reports, per route, the caller's own window (used / remaining / reset)
    plus the shared capacity water level. Purely read-only: it never
    acquires quota or capacity and never mutates window state, so polling
    this endpoint is free.
    """
    services = _services(request)
    requested_version = request.headers.get("x-api-version")
    result = admit_introspect(
        requested_version=requested_version,
        api_key=request.headers.get("x-api-key"),
        route=ROUTE_USAGE, services=services)
    cred = result.credential
    now = services["clock"].now()

    routes = {}
    for route, default_limit in DEFAULT_LIMITS.items():
        own_limit = cred.quota_limits.get(route, default_limit)
        own = services["quota"].peek(cred.key_id, route, own_limit, now)
        cap_limit = ROUTE_CAPACITY[route]
        cap = services["capacity"].peek(SHARED_KEY_ID, route, cap_limit, now)
        routes[route] = {
            "quota": {
                "used": own.used,
                "limit": own.limit,
                "remaining": max(0, own.limit - own.used),
                "reset_at": own.reset_at.isoformat(),
            },
            "shared_capacity": {
                "used": cap.used,
                "limit": cap.limit,
                "remaining": max(0, cap.limit - cap.used),
                "reset_at": cap.reset_at.isoformat(),
            },
        }

    _audit(services, key_id=cred.key_id, route=ROUTE_USAGE,
           requested=requested_version or DEFAULT_VERSION,
           resolved=result.version.version, decision="inspect", reason=None,
           detail={"key_id": cred.key_id,
                   "routes": {r: v["quota"]["used"] for r, v in routes.items()}})
    data = {
        "key_id": cred.key_id,
        "window_seconds": services["quota"].window_seconds,
        "routes": routes,
    }
    headers = _deprecation(result, {"X-Api-Version": result.version.version})
    return JSONResponse(_envelope(result.version.version, data),
                        headers=headers)


_CREATE_LOCK = threading.Lock()


@router.post("/widgets")
async def create_widget(request: Request):
    services = _services(request)
    payload = await request.json()
    idem_key = request.headers.get("idempotency-key")
    requested_version = request.headers.get("x-api-version")
    api_key = request.headers.get("x-api-key")

    if not idem_key:
        # Version/auth are still checked first for consistent error ordering,
        # but the absence of a key is itself an auditable rejection.
        result = admit_pre_quota(
            requested_version=requested_version, api_key=api_key,
            route=ROUTE_WIDGETS_CREATE, services=services)
        _audit(services, key_id=result.credential.key_id,
               route=ROUTE_WIDGETS_CREATE,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="reject",
               reason="missing_idempotency_key")
        raise GatewayError(
            400, RejectReason.BAD_REQUEST,
            "Write requests require an Idempotency-Key header.")

    # Stages 1-3 only: replays/conflicts must never consume quota.
    result = admit_pre_quota(
        requested_version=requested_version, api_key=api_key,
        route=ROUTE_WIDGETS_CREATE, services=services)
    cred = result.credential
    store = services["idempotency"]
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    state, record = store.begin(
        idem_key, cred.key_id, ROUTE_WIDGETS_CREATE, request_hash)

    if state is CONFLICT:
        _audit(services, key_id=cred.key_id, route=ROUTE_WIDGETS_CREATE,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="reject",
               reason=RejectReason.IDEMPOTENCY_CONFLICT.value,
               detail={"key_id": cred.key_id, "idempotency_key": idem_key,
                       "original_route": record.route if record else None})
        raise GatewayError(
            409, RejectReason.IDEMPOTENCY_CONFLICT,
            "Idempotency-Key was already used with a different request.",
            extra={"key_id": cred.key_id, "idempotency_key": idem_key,
                   "original_route": record.route if record else None,
                   "original_request_hash": (record.request_hash if record
                                             else "in-flight")})
    if state is INFLIGHT:
        _audit(services, key_id=cred.key_id, route=ROUTE_WIDGETS_CREATE,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="reject",
               reason="idempotency_inflight",
               detail={"idempotency_key": idem_key})
        raise GatewayError(
            409, RejectReason.IDEMPOTENCY_CONFLICT,
            "A request with this Idempotency-Key is still in progress.",
            extra={"idempotency_key": idem_key, "retryable": True},
            headers={"Retry-After": "1"})
    if state is REPLAY:
        _audit(services, key_id=cred.key_id, route=ROUTE_WIDGETS_CREATE,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="replay",
               reason="idempotent_replay",
               detail={"key_id": cred.key_id, "idempotency_key": idem_key})
        body = _envelope(result.version.version, record.body, replay=True)
        replay_headers = _deprecation(result, _rate_headers(
            result, used=0, limit=cred.quota_limits[ROUTE_WIDGETS_CREATE]))
        replay_headers["Idempotent-Replay"] = "true"
        return JSONResponse(status_code=record.status_code, content=body,
                            headers=replay_headers)

    # state == STORED: only the first writer contends for budget.
    quota_result, _cap = _write_budget(
        services, result, cred, idem_key, requested_version, 1,
        ROUTE_WIDGETS_CREATE)

    try:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise GatewayError(422, RejectReason.BAD_REQUEST,
                               "Field 'name' is required and must be a string.")
        with _CREATE_LOCK:
            next_id = f"w-{len(services['widgets']) + 1}"
            widget = {"id": next_id, "name": name,
                     "versioned_payload": "same-on-every-version"}
            services["widgets"][next_id] = widget
        data = {"widget": widget}
        completed = store.complete(idem_key, 201, data)
    except GatewayError:
        store.fail(idem_key)
        _rollback_write_budget(services, cred, 1)
        raise
    body = _envelope(result.version.version, completed.body)
    headers = _deprecation(result, _rate_headers(
        result, used=quota_result.used, limit=quota_result.limit))
    headers["Idempotency-Key"] = idem_key
    return JSONResponse(status_code=201, content=body, headers=headers)


@router.post("/widgets/batch")
async def create_widgets_batch(request: Request):
    """Bulk create: quota/capacity are charged per record actually written.

    The batch is all-or-nothing -- insufficient budget rejects the whole
    request before anything is held, and a business failure rolls back
    every unit acquired. Charged against the same ``widgets:create``
    budget as single creates (a batch is just a bulk create).
    """
    services = _services(request)
    payload = await request.json()
    idem_key = request.headers.get("idempotency-key")
    requested_version = request.headers.get("x-api-version")
    api_key = request.headers.get("x-api-key")

    if not idem_key:
        result = admit_pre_quota(
            requested_version=requested_version, api_key=api_key,
            route=ROUTE_WIDGETS_CREATE, services=services)
        _audit(services, key_id=result.credential.key_id,
               route=ROUTE_WIDGETS_BATCH,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="reject",
               reason="missing_idempotency_key")
        raise GatewayError(
            400, RejectReason.BAD_REQUEST,
            "Write requests require an Idempotency-Key header.")

    # Stages 1-3 only: replays/conflicts must never consume quota.
    result = admit_pre_quota(
        requested_version=requested_version, api_key=api_key,
        route=ROUTE_WIDGETS_CREATE, services=services)
    cred = result.credential

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise GatewayError(
            400, RejectReason.BAD_REQUEST,
            "Field 'items' must be a non-empty list of records.")
    if len(items) > MAX_BATCH_SIZE:
        raise GatewayError(
            400, RejectReason.BAD_REQUEST,
            f"Batch size {len(items)} exceeds the maximum {MAX_BATCH_SIZE}.",
            extra={"max_batch_size": MAX_BATCH_SIZE})
    amount = len(items)

    store = services["idempotency"]
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    state, record = store.begin(
        idem_key, cred.key_id, ROUTE_WIDGETS_BATCH, request_hash)

    if state is CONFLICT:
        _audit(services, key_id=cred.key_id, route=ROUTE_WIDGETS_BATCH,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="reject",
               reason=RejectReason.IDEMPOTENCY_CONFLICT.value,
               detail={"key_id": cred.key_id, "idempotency_key": idem_key,
                       "original_route": record.route if record else None})
        raise GatewayError(
            409, RejectReason.IDEMPOTENCY_CONFLICT,
            "Idempotency-Key was already used with a different request.",
            extra={"key_id": cred.key_id, "idempotency_key": idem_key,
                   "original_route": record.route if record else None,
                   "original_request_hash": (record.request_hash if record
                                             else "in-flight")})
    if state is INFLIGHT:
        _audit(services, key_id=cred.key_id, route=ROUTE_WIDGETS_BATCH,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="reject",
               reason="idempotency_inflight",
               detail={"idempotency_key": idem_key})
        raise GatewayError(
            409, RejectReason.IDEMPOTENCY_CONFLICT,
            "A request with this Idempotency-Key is still in progress.",
            extra={"idempotency_key": idem_key, "retryable": True},
            headers={"Retry-After": "1"})
    if state is REPLAY:
        _audit(services, key_id=cred.key_id, route=ROUTE_WIDGETS_BATCH,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="replay",
               reason="idempotent_replay",
               detail={"key_id": cred.key_id, "idempotency_key": idem_key})
        body = _envelope(result.version.version, record.body, replay=True)
        replay_headers = _deprecation(result, _rate_headers(
            result, used=0, limit=cred.quota_limits[ROUTE_WIDGETS_CREATE]))
        replay_headers["Idempotent-Replay"] = "true"
        return JSONResponse(status_code=record.status_code, content=body,
                            headers=replay_headers)

    # state == STORED: whole batch contends for budget as one atomic unit.
    quota_result, _cap = _write_budget(
        services, result, cred, idem_key, requested_version, amount,
        ROUTE_WIDGETS_BATCH)

    try:
        names = []
        for i, item in enumerate(items):
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name:
                raise GatewayError(
                    422, RejectReason.BAD_REQUEST,
                    f"Record {i}: field 'name' is required and must be a "
                    "non-empty string.",
                    extra={"record_index": i})
            names.append(name)
        with _CREATE_LOCK:
            created = []
            for name in names:
                next_id = f"w-{len(services['widgets']) + 1}"
                widget = {"id": next_id, "name": name,
                          "versioned_payload": "same-on-every-version"}
                services["widgets"][next_id] = widget
                created.append(widget)
        data = {"widgets": created, "written": len(created)}
        completed = store.complete(idem_key, 201, data)
    except GatewayError:
        store.fail(idem_key)
        _rollback_write_budget(services, cred, amount)
        raise
    body = _envelope(result.version.version, completed.body)
    headers = _deprecation(result, _rate_headers(
        result, used=quota_result.used, limit=quota_result.limit))
    headers["Idempotency-Key"] = idem_key
    return JSONResponse(status_code=201, content=body, headers=headers)


def install_error_handlers(app) -> None:
    @app.exception_handler(GatewayError)
    async def _on_gateway_error(_request: Request, exc: GatewayError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.extra),
            headers=exc.headers)

    @app.exception_handler(Exception)
    async def _on_unexpected(_request: Request, exc: Exception):
        logging.getLogger("gateway").exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=error_body(RejectReason.INTERNAL_ERROR,
                               "Internal server error."))


def install_routes(app) -> None:
    app.include_router(router)
    install_error_handlers(app)
