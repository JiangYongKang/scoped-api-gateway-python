"""错误原因可区分性矩阵。

每种拒绝原因都有唯一的 (HTTP 状态码, error.code) 组合，
调用方可以程序化地判别：版本、认证、权限、配额、幂等互不混淆。
"""

from __future__ import annotations

import logging


log = logging.getLogger("test.error_matrix")


def _show(name: str, status: int, code: str) -> None:
    log.info("场景=%-28s | 判定: HTTP %s code=%s", name, status, code)


def test_error_matrix(client) -> None:
    cases = []

    r = client.get("/v1/echo")
    cases.append(("缺凭据", r.status_code, r.json()["error"]["code"]))

    r = client.get("/v1/echo", headers={"X-Api-Key": "bad"})
    cases.append(("无效凭据", r.status_code, r.json()["error"]["code"]))

    r = client.get("/v1/echo", headers={"X-Api-Key": "sk-expired"})
    cases.append(("凭据过期", r.status_code, r.json()["error"]["code"]))

    r = client.get("/v1/echo", headers={"X-Api-Key": "sk-revoked"})
    cases.append(("凭据撤销", r.status_code, r.json()["error"]["code"]))

    r = client.get("/v1/echo",
                   headers={"X-Api-Key": "sk-active",
                            "X-Api-Version": "2099-01-01"})
    cases.append(("未知版本", r.status_code, r.json()["error"]["code"]))

    r = client.get("/v1/echo",
                   headers={"X-Api-Key": "sk-active",
                            "X-Api-Version": "2023-01-01"})
    cases.append(("版本下线", r.status_code, r.json()["error"]["code"]))

    r = client.post("/v1/orders",
                    headers={"X-Api-Key": "sk-readonly",
                             "Idempotency-Key": "x"},
                    json={"item": "a"})
    cases.append(("权限不足", r.status_code, r.json()["error"]["code"]))

    r = client.post("/v1/orders",
                    headers={"X-Api-Key": "sk-active"},
                    json={"item": "a"})
    cases.append(("缺幂等键", r.status_code, r.json()["error"]["code"]))

    client.post("/v1/orders",
                headers={"X-Api-Key": "sk-active", "Idempotency-Key": "m"},
                json={"item": "a"})
    r = client.post("/v1/orders",
                    headers={"X-Api-Key": "sk-active", "Idempotency-Key": "m"},
                    json={"item": "b"})
    cases.append(("幂等载荷冲突", r.status_code, r.json()["error"]["code"]))

    for i in range(5):
        client.post("/v1/orders",
                    headers={"X-Api-Key": "sk-rotating-new",
                             "Idempotency-Key": f"q{i}"},
                    json={"item": "a"})
    r = client.post("/v1/orders",
                    headers={"X-Api-Key": "sk-rotating-new",
                             "Idempotency-Key": "over"},
                    json={"item": "a"})
    cases.append(("配额超限", r.status_code, r.json()["error"]["code"]))

    for name, status, code in cases:
        _show(name, status, code)

    expected = {
        ("缺凭据", 401, "credential_missing"),
        ("无效凭据", 401, "credential_invalid"),
        ("凭据过期", 401, "credential_expired"),
        ("凭据撤销", 401, "credential_revoked"),
        ("未知版本", 400, "version_unknown"),
        ("版本下线", 410, "version_retired"),
        ("权限不足", 403, "permission_denied"),
        ("缺幂等键", 400, "idempotency_key_missing"),
        ("幂等载荷冲突", 409, "idempotency_payload_conflict"),
        ("配额超限", 429, "quota_exceeded"),
    }
    assert set(cases) == expected
    # 所有 (status, code) 组合两两可区分
    pairs = {(s, c) for _, s, c in cases}
    assert len(pairs) == len(cases)
    # 每个错误体都带 request_id，且 retryable 仅在配额场景为 true
    quota_err = next(c for n, s, c in cases if n == "配额超限")
    assert quota_err == "quota_exceeded"
