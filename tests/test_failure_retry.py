"""业务失败后的配额回滚与幂等键释放测试。

- 下游返回 5xx：配额回滚（可重试不被误判超限）、幂等键释放（同键可再提交）；
- 超限被拒的请求不计数，窗口用量保持在 limit。
"""

from __future__ import annotations

import logging

log = logging.getLogger("test.failure")

H = {"X-Api-Key": "sk-active"}


def test_5xx_releases_quota_and_idempotency_key(client) -> None:
    key = {"Idempotency-Key": "FLAKY-1"}

    # 1) 同键连续失败 3 次：每次都应真实执行业务并返回 500（键被释放）
    for i in range(3):
        r = client.post("/v1/flaky?fail_until=99",
                        headers={**H, **key}, json={"item": "x"})
        log.info("第 %d 次提交（预期5xx）| 判定: %s", i + 1, r.status_code)
        assert r.status_code == 500
    attempts = client.get("/__test/state").json()[
        "side_effect_counter"]["flaky_attempts"]
    assert attempts == 3, "失败后幂等键必须释放，每次都应真实执行"

    # 2) 失败期间配额全部回滚：仍能连续拿到 5 个成功槽位
    for i in range(5):
        r = client.post("/v1/flaky",
                        headers={**H, "Idempotency-Key": f"FLAKY-OK-{i}"},
                        json={"item": "x"})
        assert r.status_code == 200, r.text
    # 第 6 个新键 -> 429：证明 5xx 回滚没有让计数虚高/虚低
    r = client.post("/v1/flaky",
                    headers={**H, "Idempotency-Key": "FLAKY-OVER"},
                    json={"item": "x"})
    log.info("5次成功后第6新键 | 判定: %s code=%s",
             r.status_code, r.json()["error"]["code"])
    assert r.status_code == 429

    # 3) 等窗口/重置后，验证同键失败→成功→重放的终态固化
    client.post("/__test/reset")
    r_ok = client.post("/v1/flaky",
                       headers={**H, **key}, json={"item": "x"})
    log.info("同键失败后重试成功 | 判定: %s body=%s",
             r_ok.status_code, r_ok.json())
    assert r_ok.status_code == 200
    r_replay = client.post("/v1/flaky",
                           headers={**H, **key}, json={"item": "x"})
    assert r_replay.status_code == 200
    assert r_replay.headers["idempotent-replayed"] == "true"
    assert r_replay.json() == r_ok.json()
    assert client.get("/__test/state").json()[
        "side_effect_counter"]["flaky_attempts"] == 1


def test_quota_rejected_request_does_not_consume_quota(client) -> None:
    """超限被拒不计数：用满 5 后再被拒任意次，窗口用量仍是 5。"""
    for i in range(5):
        r = client.post("/v1/orders",
                        headers={**H, "Idempotency-Key": f"Q-{i}"},
                        json={"item": "x"})
        assert r.status_code == 201
    rejected = []
    for i in range(10):
        r = client.post("/v1/orders",
                        headers={**H, "Idempotency-Key": f"Q-DENY-{i}"},
                        json={"item": "x"})
        rejected.append(r.status_code)
        assert "Retry-After" in r.headers
    assert set(rejected) == {429}
    state = client.get("/__test/state").json()
    log.info("10 次超限拒绝后 用量=%s 副作用=%s",
             state["quota_used"]["/v1/orders"],
             state["side_effect_counter"]["orders_created"])
    assert state["quota_used"]["/v1/orders"] == 5
    assert state["side_effect_counter"]["orders_created"] == 5


def test_quota_denied_first_request_does_not_hold_idempotency_key(client) -> None:
    """回归：成为幂等首请求后又被 429 拒绝时，必须释放幂等键；
    窗口恢复后同一幂等键可正常提交成功。"""
    # 用满 /v1/orders 的 5 个槽位
    for i in range(5):
        r = client.post("/v1/orders",
                        headers={**H, "Idempotency-Key": f"fill-{i}"},
                        json={"item": "x"})
        assert r.status_code == 201
    # 新键在配额已满时被拒（此刻它是该键的首请求）
    r = client.post("/v1/orders",
                    headers={**H, "Idempotency-Key": "AFTER-429"},
                    json={"item": "x"})
    log.info("配额满时新键 AFTER-429 | 判定: %s code=%s",
             r.status_code, r.json()["error"]["code"])
    assert r.status_code == 429

    # 仅重置配额窗口（保留幂等记录），模拟窗口滚动后重试
    client.app.state.components.quotas.reset()
    r2 = client.post("/v1/orders",
                     headers={**H, "Idempotency-Key": "AFTER-429"},
                     json={"item": "x"})
    log.info("窗口恢复后同键重试 | 判定: %s body=%s",
             r2.status_code, r2.json() if r2.status_code < 400 else r2.text)
    assert r2.status_code == 201, r2.text
    # 再重放一次：返回同一结果，无新副作用，且不扣配额
    r3 = client.post("/v1/orders",
                     headers={**H, "Idempotency-Key": "AFTER-429"},
                     json={"item": "x"})
    assert r3.status_code == 201
    assert r3.json() == r2.json()
    assert r3.headers["idempotent-replayed"] == "true"
    state = client.get("/__test/state").json()
    assert state["quota_used"]["/v1/orders"] == 1
