"""准入层 ASGI 中间件：串联版本协商、认证授权、配额与幂等。

判定顺序（每一步拒绝都与其它原因可区分，均独立审计）：
1. 版本协商：未指定 -> 默认版本；未知 -> version_unknown(400)；
   下线 -> version_retired(410)；弃用版本放行并带 Deprecation/Sunset；
2. 认证：missing/invalid/expired/revoked（401）；
3. 授权：scope 不足（403）；
4. 幂等（仅写方法）：同键重放不执行业务；不同载荷 409；同键并发等待；
5. 配额：超限 429 带 Retry-After（幂等重放不计数）；
6. 执行业务：业务 5xx 回滚配额、释放幂等键（允许重试）；
7. 全程审计（只记录凭据 ID 与密钥指纹，绝不记录明文密钥）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from gateway import idempotency as idem
from gateway.audit import AuditEntry, AuditLogger, key_fingerprint
from gateway.credentials import CredentialStore
from gateway.errors import GatewayError
from gateway.idempotency import (
    IDEMPOTENCY_HEADER,
    REPLAY_HEADER as IDEMPOTENCY_REPLAY_HEADER,
    IdempotencyStore,
)
from gateway.quotas import QuotaManager
from gateway.versions import (
    API_VERSION_HEADER,
    DEPRECATION_HEADER,
    LINK_HEADER,
    SUNSET_HEADER,
    VersionInfo,
    VersionRegistry,
)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

REQUEST_ID_HEADER = "X-Request-Id"
API_KEY_HEADER = "X-Api-Key"
RETRY_AFTER_HEADER = "Retry-After"


@dataclass
class AdmissionComponents:
    versions: VersionRegistry
    credentials: CredentialStore
    quotas: QuotaManager
    idempotency: IdempotencyStore
    audit: AuditLogger
    route_scopes: dict[str, str]  # route -> 所需 scope
    public_paths: frozenset[str] = frozenset()  # 不经准入的路径
    protected_paths: frozenset[str] | None = None  # 显式受保护路径；None 表示除 public 外全部


def error_payload(
    exc: GatewayError, request_id: str, version: str | None
) -> dict[str, Any]:
    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "request_id": request_id,
            "version": version,
            "retryable": exc.retryable,
            "details": exc.details,
        }
    }


def error_response(exc: GatewayError, request_id: str, version: str | None) -> JSONResponse:
    headers = {REQUEST_ID_HEADER: request_id}
    if exc.retryable and "retry_after_seconds" in exc.details:
        headers[RETRY_AFTER_HEADER] = str(exc.details["retry_after_seconds"])
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc, request_id, version),
        headers=headers,
    )


class _BufferedSend:
    """拦截下游响应：补响应头、判定 5xx、缓冲响应体用于幂等固化。"""

    def __init__(self, send, on_start, on_body_complete) -> None:  # noqa: ANN001
        self._send = send
        self._on_start = on_start
        self._on_body_complete = on_body_complete
        self.status_code = 200
        self._body = bytearray()

    async def __call__(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status_code = message["status"]
            headers = list(message.get("headers") or [])
            extra = await self._on_start(self.status_code)
            headers.extend(
                (k.lower().encode(), v.encode()) for k, v in extra.items()
            )
            message["headers"] = headers
        elif message["type"] == "http.response.body":
            self._body.extend(message.get("body") or b"")
            if not message.get("more_body", False):
                await self._on_body_complete(self.status_code, bytes(self._body))
        await self._send(message)

    async def send(self, message: dict[str, Any]) -> None:
        await self(message)


class AdmissionMiddleware:
    def __init__(self, app: Any, components: AdmissionComponents) -> None:
        self.app = app
        self.components = components

    # -- 审计辅助 ---------------------------------------------------------
    def _deny(
        self,
        *,
        request_id: str,
        event: str,
        reason: str,
        route: str,
        version: str | None,
        credential_id: str | None = None,
        fingerprint: str | None = None,
        idempotency_key: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.components.audit.log(
            AuditEntry(
                request_id=request_id,
                event=event,
                decision="deny",
                reason=reason,
                route=route,
                version=version,
                credential_id=credential_id,
                key_fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                extra=extra or {},
            )
        )

    # -- 主流程 -----------------------------------------------------------
    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method: str = scope["method"]
        route: str = scope["path"]
        headers = Headers(scope=scope)
        comps = self.components

        # 公开路径旁路（健康检查、测试重置端点等）
        if route in comps.public_paths:
            await self.app(scope, receive, send)
            return
        # 若显式声明了受保护路径，其余路径一律旁路
        if comps.protected_paths is not None and route not in comps.protected_paths:
            await self.app(scope, receive, send)
            return

        request_id = headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        fingerprint = key_fingerprint(headers.get(API_KEY_HEADER))
        version_label: str | None = None

        def deny_and_send(
            exc: GatewayError,
            *,
            event: str,
            credential_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> JSONResponse:
            self._deny(
                request_id=request_id,
                event=event,
                reason=exc.code,
                route=route,
                version=version_label,
                credential_id=credential_id,
                fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                extra=exc.details,
            )
            return error_response(exc, request_id, version_label)

        # 1. 版本协商
        requested_version = headers.get(API_VERSION_HEADER)
        try:
            vinfo: VersionInfo = comps.versions.negotiate(requested_version)
        except GatewayError as exc:
            resp = deny_and_send(exc, event="version_negotiation")
            await resp(scope, receive, send)
            return
        version_label = vinfo.version

        # 2. 认证
        try:
            credential = comps.credentials.authenticate(headers.get(API_KEY_HEADER))
        except GatewayError as exc:
            resp = deny_and_send(exc, event="authentication")
            await resp(scope, receive, send)
            return

        # 3. 授权（未配置 scope 的路由默认放行）
        required_scope = comps.route_scopes.get(route)
        if required_scope is not None:
            try:
                comps.credentials.authorize(credential, required_scope)
            except GatewayError as exc:
                resp = deny_and_send(exc, event="authorization",
                                     credential_id=credential.credential_id)
                await resp(scope, receive, send)
                return

        # 4. 幂等（仅写方法）
        idem_key = headers.get(IDEMPOTENCY_HEADER)
        idem_record = None
        replay_status = 200
        replay_json: dict[str, Any] | None = None
        if method in WRITE_METHODS:
            body = await self._read_body(receive)

            async def replay_receive():  # 保证下游有 body 可读
                return {"type": "http.request", "body": body, "more_body": False}

            try:
                if not idem_key:
                    raise GatewayError(
                        "写操作必须提供 Idempotency-Key 请求头",
                        code="idempotency_key_missing",
                        status_code=400,
                    )
                payload_hash = idem.hash_payload(
                    body, content_type=headers.get("content-type")
                )
                idem_record, is_first = await comps.idempotency.begin_or_wait(
                    idem_key, credential.credential_id, route, payload_hash
                )
            except GatewayError as exc:
                resp = deny_and_send(
                    exc, event="idempotency",
                    credential_id=credential.credential_id,
                    idempotency_key=idem_key,
                )
                await resp(scope, receive, send)
                return

            if not is_first:
                replay_status = idem_record.status_code
                replay_json = idem_record.response or {}
                comps.audit.log(
                    AuditEntry(
                        request_id=request_id,
                        event="idempotency_replay",
                        decision="allow",
                        reason="replayed",
                        route=route,
                        version=version_label,
                        credential_id=credential.credential_id,
                        key_fingerprint=fingerprint,
                        idempotency_key=idem_key,
                        extra={"replay": True},
                    )
                )
            receive = replay_receive

        # 5. 配额（重放不计数）
        quota_acquired = False
        used: int | None = None
        if replay_json is None:
            try:
                used = await comps.quotas.acquire(credential.credential_id, route)
                quota_acquired = True
            except GatewayError as exc:
                # 首请求因配额被拒时业务并未执行：必须释放幂等键，
                # 否则同键的后续请求会永久等待；该次拒绝本就不计数。
                if idem_record is not None:
                    comps.idempotency.fail(idem_record)
                resp = deny_and_send(
                    exc, event="quota", credential_id=credential.credential_id,
                    idempotency_key=idem_key,
                )
                await resp(scope, receive, send)
                return

            comps.audit.log(
                AuditEntry(
                    request_id=request_id,
                    event="request",
                    decision="allow",
                    reason="admitted",
                    route=route,
                    version=version_label,
                    credential_id=credential.credential_id,
                    key_fingerprint=fingerprint,
                    idempotency_key=idem_key,
                    extra={"quota_used": used, "replay": False},
                )
            )

        # 6. 重放走短路响应
        if replay_json is not None:
            replay_headers = {
                REQUEST_ID_HEADER: request_id,
                API_VERSION_HEADER: vinfo.version,
                IDEMPOTENCY_REPLAY_HEADER: "true",
            }
            replay_headers.update(self._version_headers(vinfo))
            resp = JSONResponse(
                status_code=replay_status,
                content=replay_json,
                headers=replay_headers,
            )
            await resp(scope, receive, send)
            return

        # 7. 调用下游；缓冲响应以补头、捕获 5xx 做配额回滚与幂等释放
        captured_status = 200
        captured_body = b""

        async def on_start(status_code: int) -> dict[str, str]:
            nonlocal captured_status
            captured_status = status_code
            return {REQUEST_ID_HEADER: request_id,
                    API_VERSION_HEADER: vinfo.version,
                    **self._version_headers(vinfo)}

        async def on_body_complete(status_code: int, body: bytes) -> None:
            nonlocal captured_status, captured_body
            captured_status = status_code
            captured_body = body

        buffered = _BufferedSend(send, on_start, on_body_complete)
        try:
            await self.app(scope, receive, buffered.send)
        except Exception:
            # 下游未产出响应即抛异常：回滚配额、释放幂等键后继续上抛，
            # 由外层 ServerErrorMiddleware 生成 500。
            if quota_acquired:
                await comps.quotas.release(credential.credential_id, route)
            if idem_record is not None:
                comps.idempotency.fail(idem_record)
            self._deny(
                request_id=request_id,
                event="business_exception",
                reason="unhandled_exception",
                route=route,
                version=version_label,
                credential_id=credential.credential_id,
                fingerprint=fingerprint,
                idempotency_key=idem_key,
            )
            raise

        if captured_status >= 500:
            if quota_acquired:
                await comps.quotas.release(credential.credential_id, route)
            if idem_record is not None:
                comps.idempotency.fail(idem_record)
            comps.audit.log(
                AuditEntry(
                    request_id=request_id,
                    event="business_failure",
                    decision="allow",
                    reason=f"http_{captured_status}",
                    route=route,
                    version=version_label,
                    credential_id=credential.credential_id,
                    key_fingerprint=fingerprint,
                    idempotency_key=idem_key,
                )
            )
        elif idem_record is not None:
            # 非 5xx 视为终态并固化原始响应体，供后续重放返回一致结果
            try:
                frozen = json.loads(captured_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                frozen = {"raw_body": captured_body.decode("utf-8", "replace")}
            comps.idempotency.complete(idem_record, captured_status, frozen)

    @staticmethod
    async def _read_body(receive) -> bytes:  # noqa: ANN001
        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body") or b"")
            more = message.get("more_body", False)
        return b"".join(chunks)

    @staticmethod
    def _version_headers(vinfo: VersionInfo) -> dict[str, str]:
        from gateway.versions import VersionStatus

        if vinfo.status is not VersionStatus.DEPRECATED:
            return {}
        headers = {
            DEPRECATION_HEADER: "true",
        }
        if vinfo.sunset:
            headers[SUNSET_HEADER] = vinfo.sunset
        if vinfo.successor:
            headers[LINK_HEADER] = (
                f'</docs/versions>; rel="successor-version"; '
                f'title="{vinfo.successor}"'
            )
        return headers
