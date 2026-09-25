"""Business routes shared, unchanged in semantics, across all versions.

Read route order: version -> credential -> scope -> quota (see ``admit``).

Write route order:

    version -> credential -> scope
      -> idempotency begin (replay / conflict return HERE, never touching quota)
      -> quota acquire (first writer only)
        -> business side effect (exactly once per key)
      -> idempotency complete
        -> failure before completion: quota rollback + key release

Replays therefore consume no quota and return the first response verbatim;
conflicts likewise leave quota and widget state untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.admission import admit, admit_pre_quota, _audit
from gateway.errors import GatewayError, error_body
from gateway.idempotency import CONFLICT, INFLIGHT, REPLAY
from gateway.models import RejectReason
from gateway.store import (ROUTE_WIDGET_DETAIL, ROUTE_WIDGETS_CREATE,
                           ROUTE_WIDGETS_LIST)
from gateway.versioning import DEFAULT_VERSION, deprecation_headers

router = APIRouter()


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

    # state == STORED: only the first writer contends for quota.
    quota_result = services["quota"].try_acquire(
        cred.key_id, ROUTE_WIDGETS_CREATE,
        cred.quota_limits[ROUTE_WIDGETS_CREATE], services["clock"].now())
    if not quota_result.allowed:
        # Release the reservation so the caller can retry after the window.
        store.fail(idem_key)
        _audit(services, key_id=cred.key_id, route=ROUTE_WIDGETS_CREATE,
               requested=requested_version or DEFAULT_VERSION,
               resolved=result.version.version, decision="reject",
               reason=RejectReason.QUOTA_EXCEEDED.value,
               before=quota_result.used, after=quota_result.used,
               detail={"key_id": cred.key_id, "idempotency_key": idem_key,
                       "retry_after_seconds": quota_result.retry_after})
        raise GatewayError(
            429, RejectReason.QUOTA_EXCEEDED,
            "Per-credential route quota exceeded.",
            extra={"key_id": cred.key_id, "route": ROUTE_WIDGETS_CREATE,
                   "used": quota_result.used, "limit": quota_result.limit,
                   "retry_after_seconds": quota_result.retry_after,
                   "reset_at": quota_result.reset_at.isoformat()},
            headers={"Retry-After": str(quota_result.retry_after)})

    _audit(services, key_id=cred.key_id, route=ROUTE_WIDGETS_CREATE,
           requested=requested_version or DEFAULT_VERSION,
           resolved=result.version.version, decision="allow", reason=None,
           before=quota_result.used - 1, after=quota_result.used,
           detail={"key_id": cred.key_id, "limit": quota_result.limit,
                   "idempotency_key": idem_key})

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
        services["quota"].rollback(cred.key_id, ROUTE_WIDGETS_CREATE)
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
