"""FastAPI 应用工厂：装配示例业务路由与网关准入中间件。

所有内存状态保存在 app.state，不同 create_app() 实例彼此隔离，
便于测试与本地多实例验证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from gateway.admission import AdmissionComponents, AdmissionMiddleware
from gateway.audit import AuditLogger
from gateway.credentials import Credential, CredentialStore, Secret
from gateway.idempotency import IdempotencyStore
from gateway.quotas import QuotaManager, QuotaPolicy
from gateway.versions import default_registry


class OrderIn(BaseModel):
    item: str
    quantity: int = 1


def _default_credential_store() -> CredentialStore:
    now = datetime.now(timezone.utc)
    return CredentialStore(
        [
            Credential(
                credential_id="cred-active",
                secrets=[Secret("sk-active", now - timedelta(days=1), None)],
                scopes=frozenset({"echo:read", "orders:write"}),
            ),
            Credential(
                credential_id="cred-readonly",
                secrets=[Secret("sk-readonly", now - timedelta(days=1), None)],
                scopes=frozenset({"echo:read"}),
            ),
            # 轮换中：新旧密钥有效期重叠一天
            Credential(
                credential_id="cred-rotating",
                secrets=[
                    Secret(
                        "sk-rotating-old",
                        now - timedelta(days=400),
                        now + timedelta(days=1),
                    ),
                    Secret("sk-rotating-new", now - timedelta(days=1), None),
                ],
                scopes=frozenset({"echo:read", "orders:write"}),
            ),
            Credential(
                credential_id="cred-expired",
                secrets=[
                    Secret(
                        "sk-expired",
                        now - timedelta(days=400),
                        now - timedelta(days=1),
                    )
                ],
                scopes=frozenset({"echo:read"}),
            ),
            Credential(
                credential_id="cred-revoked",
                secrets=[Secret("sk-revoked", now - timedelta(days=1), None)],
                scopes=frozenset({"echo:read", "orders:write"}),
                revoked=True,
            ),
        ]
    )


def create_app(components: AdmissionComponents | None = None) -> FastAPI:
    application = FastAPI(title="Scoped API Gateway")

    if components is None:
        store = _default_credential_store()
        audit = AuditLogger()
        # 把全部明文密钥注册为审计禁出值：任何审计记录误含明文都会立即报错
        for cred in store.list_credentials():
            for sec in cred.secrets:
                audit.register_secret(sec.secret)
        components = AdmissionComponents(
            versions=default_registry(),
            credentials=store,
            quotas=QuotaManager(
                {
                    "/v1/echo": QuotaPolicy(limit=30, window_seconds=60),
                    "/v1/orders": QuotaPolicy(limit=5, window_seconds=60),
                    "/v1/flaky": QuotaPolicy(limit=5, window_seconds=60),
                }
            ),
            idempotency=IdempotencyStore(),
            audit=audit,
            route_scopes={
                "/v1/echo": "echo:read",
                "/v1/orders": "orders:write",
                "/v1/flaky": "orders:write",
            },
            protected_paths=frozenset({"/v1/echo", "/v1/orders", "/v1/flaky"}),
        )

    application.state.components = components
    # 业务副作用计数（测试用于断言幂等不产生额外副作用）
    application.state.side_effect_counter = {"orders_created": 0, "flaky_attempts": 0}

    @application.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/v1/echo")
    def echo(message: str = "hello") -> dict[str, Any]:
        # 同一业务语义在所有受支持版本下返回一致结果；版本由网关注入响应头
        return {"semantic": "echo-v1", "echo": message}

    @application.post("/v1/orders", status_code=201)
    def create_order(payload: OrderIn) -> dict[str, Any]:
        application.state.side_effect_counter["orders_created"] += 1
        n = application.state.side_effect_counter["orders_created"]
        return {
            "semantic": "order-v1",
            "order_id": f"ORD-{n:04d}",
            "item": payload.item,
            "quantity": payload.quantity,
        }

    @application.post("/v1/flaky", status_code=200)
    def flaky_write(payload: OrderIn, fail_until: int = 0):
        # 前 fail_until 次调用返回 500（模拟可重试的业务失败）
        from starlette.responses import JSONResponse

        application.state.side_effect_counter["flaky_attempts"] += 1
        n = application.state.side_effect_counter["flaky_attempts"]
        if n <= fail_until:
            return JSONResponse(
                status_code=500, content={"error": {"code": "transient_failure"}}
            )
        return {"semantic": "flaky-v1", "ok": True, "attempt": n}

    @application.post("/__test/reset")
    def reset_state() -> dict[str, str]:
        components.quotas.reset()
        components.idempotency.reset()
        components.audit.reset()
        application.state.side_effect_counter["orders_created"] = 0
        application.state.side_effect_counter["flaky_attempts"] = 0
        return {"status": "reset"}

    @application.get("/__test/state")
    def get_state() -> dict[str, Any]:
        return {
            "side_effect_counter": dict(application.state.side_effect_counter),
            "quota_used": {
                route: components.quotas.used("cred-active", route)
                for route in ("/v1/echo", "/v1/orders")
            },
            "audit_events": [
                {
                    "request_id": e.request_id,
                    "event": e.event,
                    "decision": e.decision,
                    "reason": e.reason,
                    "credential_id": e.credential_id,
                    "route": e.route,
                    "version": e.version,
                    "idempotency_key": e.idempotency_key,
                    "key_fingerprint": e.key_fingerprint,
                    "extra": e.extra,
                }
                for e in components.audit.entries()
            ],
        }

    application.add_middleware(AdmissionMiddleware, components=components)
    return application
