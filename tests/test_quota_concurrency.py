"""配额并发竞争测试。

要求：每凭据按路由的配额在并发下准确；
超限返回 429 + retry_after；被拒绝/失败/重放不计数、不漏计。
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from gateway.app import create_app
from gateway.errors import QuotaExceededError
from gateway.quotas import QuotaManager, QuotaPolicy

log = logging.getLogger("test.quota")


@pytest.mark.asyncio
async def test_quota_concurrent_race_exact_count() -> None:
    """limit=10 时 100 个并发获取：恰好 10 成功、90 被拒，计数=10。"""
    qm = QuotaManager({"/r": QuotaPolicy(limit=10, window_seconds=60)})
    results = await asyncio.gather(
        *[qm.acquire("cred-x", "/r") for _ in range(100)],
        return_exceptions=True,
    )
    allowed = [r for r in results if not isinstance(r, BaseException)]
    denied = [r for r in results if isinstance(r, QuotaExceededError)]
    log.info("输入: 100 并发, limit=10 | 判定: 成功=%s 拒绝=%s 最终计数=%s",
             len(allowed), len(denied), qm.used("cred-x", "/r"))
    assert len(allowed) == 10
    assert len(denied) == 90
    assert qm.used("cred-x", "/r") == 10
    # 每个成功返回的已用量序号唯一且覆盖 1..10（无重复计数）
    assert sorted(allowed) == list(range(1, 11))
    # 429 详情可重试
    assert denied[0].status_code == 429
    assert denied[0].details["retry_after_seconds"] >= 1
    assert denied[0].retryable is True


@pytest.mark.asyncio
async def test_quota_released_on_failure_can_retry() -> None:
    """业务失败回滚后，配额可重新获取；计数不漏不重。"""
    qm = QuotaManager({"/r": QuotaPolicy(limit=2, window_seconds=60)})
    await qm.acquire("c", "/r")
    await qm.acquire("c", "/r")
    with pytest.raises(QuotaExceededError):
        await qm.acquire("c", "/r")
    await qm.release("c", "/r")  # 模拟一次业务 5xx 回滚
    assert await qm.acquire("c", "/r") == 2
    log.info("输入: limit=2, 成功2+拒1+回滚1 | 判定: 回滚后重新获取成功, 计数=%s",
             qm.used("c", "/r"))
    assert qm.used("c", "/r") == 2


@pytest.mark.asyncio
async def test_quota_is_per_credential_and_route() -> None:
    qm = QuotaManager({
        "/a": QuotaPolicy(limit=1, window_seconds=60),
        "/b": QuotaPolicy(limit=1, window_seconds=60),
    })
    assert await qm.acquire("c1", "/a") == 1
    assert await qm.acquire("c2", "/a") == 1  # 不同凭据
    assert await qm.acquire("c1", "/b") == 1  # 同凭据不同路由
    with pytest.raises(QuotaExceededError):
        await qm.acquire("c1", "/a")
    log.info("输入: c1/c2 x /a,/b | 判定: 配额按凭据+路由独立计数")


@pytest.mark.asyncio
async def test_http_concurrent_orders_exactly_limit_succeed(tmp_path) -> None:
    """通过真实 ASGI 栈并发打 POST /v1/orders（配额 5）：
    成功恰为 5，其余 429；副作用计数=5；429 带 Retry-After。"""
    app = create_app()
    # 用紧配额策略：替换组件中的配额管理器
    from gateway.admission import AdmissionComponents

    comps: AdmissionComponents = app.state.components
    comps.quotas = QuotaManager({"/v1/orders": QuotaPolicy(limit=5, window_seconds=60)})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as ac:
        async def post_order(i: int):
            return await ac.post(
                "/v1/orders",
                headers={"X-Api-Key": "sk-active", "Idempotency-Key": f"K-{i}"},
                json={"item": f"item-{i}"},
            )

        responses = await asyncio.gather(*[post_order(i) for i in range(20)])

    statuses = [r.status_code for r in responses]
    created = [r for r in responses if r.status_code == 201]
    limited = [r for r in responses if r.status_code == 429]
    log.info("输入: 20 并发订单(各自幂等键), limit=5 | 判定: 201=%s 429=%s",
             len(created), len(limited))
    assert statuses.count(201) == 5
    assert statuses.count(429) == 15
    # 副作用与成功数严格相等：没有重复扣减或残留
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as ac:
        state = (await ac.get("/__test/state")).json()
    assert state["side_effect_counter"]["orders_created"] == 5
    assert state["quota_used"]["/v1/orders"] == 5
    sample = limited[0].json()["error"]
    assert sample["code"] == "quota_exceeded"
    assert int(limited[0].headers["Retry-After"]) >= 1
    # 成功响应的 order_id 唯一
    ids = {r.json()["order_id"] for r in created}
    assert len(ids) == 5
