"""写操作幂等与可追溯测试。

覆盖：
- 同一幂等键重复提交返回与首次一致的结果，且无额外副作用；
- 重放不重复扣配额；
- 同键不同载荷 -> 409 idempotency_payload_conflict；
- 键跨凭据复用 -> 409 idempotency_key_reused；
- 同键并发：恰好执行一次，其余重放；
- 缺少幂等键 -> 400；
- 放行/拒绝均有审计，可关联调用方，且无明文密钥。
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from gateway.app import create_app

log = logging.getLogger("test.idempotency")

HEADERS = {"X-Api-Key": "sk-active"}


def test_replay_returns_identical_result_without_extra_side_effect(client) -> None:
    h = {**HEADERS, "Idempotency-Key": "IDEM-1"}
    r1 = client.post("/v1/orders", headers=h, json={"item": "book", "quantity": 2})
    log.info("首次提交 key=IDEM-1 | 判定: %s body=%s", r1.status_code, r1.json())
    assert r1.status_code == 201
    first = r1.json()

    r2 = client.post("/v1/orders", headers=h, json={"item": "book", "quantity": 2})
    log.info("重复提交 key=IDEM-1 | 判定: %s body=%s replayed=%s",
             r2.status_code, r2.json(), r2.headers.get("idempotent-replayed"))
    assert r2.status_code == 201
    assert r2.json() == first
    assert r2.headers["idempotent-replayed"] == "true"

    state = client.get("/__test/state").json()
    log.info("副作用计数=%s 配额用量=%s",
             state["side_effect_counter"], state["quota_used"])
    assert state["side_effect_counter"]["orders_created"] == 1
    # 首次计数 1，重放不计数
    assert state["quota_used"]["/v1/orders"] == 1


def test_replay_json_key_order_is_still_same_payload(client) -> None:
    h = {**HEADERS, "Idempotency-Key": "IDEM-2",
         "Content-Type": "application/json"}
    r1 = client.post("/v1/orders", headers=h,
                     content=b'{"quantity":1,"item":"pen"}')
    r2 = client.post("/v1/orders", headers=h,
                     content=b'{"item":"pen","quantity":1}')
    log.info("键序不同的 JSON | 判定: 首次=%s 重放=%s",
             r1.status_code, r2.status_code)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.headers["idempotent-replayed"] == "true"
    assert client.get("/__test/state").json()[
        "side_effect_counter"]["orders_created"] == 1


def test_same_key_different_payload_conflict(client) -> None:
    h = {**HEADERS, "Idempotency-Key": "IDEM-3"}
    assert client.post("/v1/orders", headers=h, json={"item": "a"}).status_code == 201
    r = client.post("/v1/orders", headers=h, json={"item": "b"})
    err = r.json()["error"]
    log.info("同键不同载荷 | 判定: %s code=%s", r.status_code, err["code"])
    assert r.status_code == 409
    assert err["code"] == "idempotency_payload_conflict"
    # 冲突请求不产生副作用、不扣配额
    state = client.get("/__test/state").json()
    assert state["side_effect_counter"]["orders_created"] == 1
    assert state["quota_used"]["/v1/orders"] == 1


def test_key_reused_by_other_credential_conflict(client) -> None:
    h1 = {"X-Api-Key": "sk-active", "Idempotency-Key": "IDEM-4"}
    h2 = {"X-Api-Key": "sk-rotating-new", "Idempotency-Key": "IDEM-4"}
    client.post("/v1/orders", headers=h1, json={"item": "a"})
    r = client.post("/v1/orders", headers=h2, json={"item": "a"})
    err = r.json()["error"]
    log.info("跨凭据复用键 | 判定: %s code=%s", r.status_code, err["code"])
    assert r.status_code == 409
    assert err["code"] == "idempotency_key_reused"


def test_write_without_idempotency_key_rejected(client) -> None:
    r = client.post("/v1/orders", headers=HEADERS, json={"item": "a"})
    err = r.json()["error"]
    log.info("写操作无幂等键 | 判定: %s code=%s", r.status_code, err["code"])
    assert r.status_code == 400
    assert err["code"] == "idempotency_key_missing"


@pytest.mark.asyncio
async def test_concurrent_same_key_executes_once() -> None:
    """10 个并发请求使用同一幂等键与同一载荷：仅 1 次副作用，全部拿到同一结果。"""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        async def call(_: int):
            return await ac.post(
                "/v1/orders",
                headers={"X-Api-Key": "sk-active", "Idempotency-Key": "DUP-1"},
                json={"item": "concurrent", "quantity": 1},
            )

        responses = await asyncio.gather(*[call(i) for i in range(10)])

    statuses = [r.status_code for r in responses]
    bodies = [r.json() for r in responses]
    replays = [r.headers.get("idempotent-replayed") == "true" for r in responses]
    log.info("10 并发同键 | 判定: 全部201=%s 重放数=%s 唯一order=%s",
             all(s == 201 for s in statuses), sum(replays),
             {b["order_id"] for b in bodies})
    assert all(s == 201 for s in statuses)
    assert sum(replays) == 9
    assert {b["order_id"] for b in bodies} == {"ORD-0001"}

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        state = (await ac.get("/__test/state")).json()
    assert state["side_effect_counter"]["orders_created"] == 1
    # 只有首次扣配额
    assert state["quota_used"]["/v1/orders"] == 1


def test_audit_trail_links_caller_without_plaintext_secret(client) -> None:
    client.post("/v1/orders",
                headers={**HEADERS, "Idempotency-Key": "AUD-1"},
                json={"item": "x"})
    # 触发一次冲突拒绝
    client.post("/v1/orders",
                headers={**HEADERS, "Idempotency-Key": "AUD-1"},
                json={"item": "y"})
    state = client.get("/__test/state").json()
    events = state["audit_events"]
    allow = [e for e in events if e["event"] == "request" and e["decision"] == "allow"]
    deny = [e for e in events if e["decision"] == "deny"]
    log.info("审计: 放行=%s 拒绝=%s",
             [(e["credential_id"], e["idempotency_key"], e["reason"]) for e in allow],
             [(e["credential_id"], e["idempotency_key"], e["reason"]) for e in deny])
    assert allow and deny
    assert allow[0]["credential_id"] == "cred-active"
    assert allow[0]["idempotency_key"] == "AUD-1"
    assert deny[0]["reason"] == "idempotency_payload_conflict"
    # 任何审计记录的文本表示都不得包含明文密钥
    blob = repr(state)
    assert "sk-active" not in blob
    assert allow[0]["key_fingerprint"].startswith("sha256:")


def test_audit_logger_rejects_plaintext_secret() -> None:
    """审计器对注册过的明文密钥做运行时拦截，误写即抛错。"""
    import pytest as _pytest

    from gateway.audit import AuditEntry, AuditLogger

    logger = AuditLogger(echo=False)
    logger.register_secret("sk-topsecret")
    # 正常记录（只有指纹）可以写出
    logger.log(AuditEntry(request_id="r", event="request",
                          key_fingerprint="sha256:abc"))
    # 误把明文放进 extra -> 运行时拒绝
    with _pytest.raises(AssertionError):
        logger.log(AuditEntry(request_id="r", event="request",
                              extra={"token": "sk-topsecret"}))
    log.info("明文密钥写入审计 -> AssertionError（运行时拦截）")
