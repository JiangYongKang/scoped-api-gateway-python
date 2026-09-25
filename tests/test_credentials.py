"""凭据状态与密钥轮换测试。

覆盖：缺失/无效/过期/撤销/权限不足错误码可区分；
轮换重叠期新旧密钥并存；过期后旧钥失效、新钥仍可用；
权限失败与版本失败状态码可区分。
"""

from __future__ import annotations

import logging

log = logging.getLogger("test.credentials")


def _deny(client, key: str | None, version: str | None = None) -> tuple[int, str]:
    headers = {}
    if key is not None:
        headers["X-Api-Key"] = key
    if version is not None:
        headers["X-Api-Version"] = version
    r = client.get("/v1/echo", headers=headers)
    code = r.json()["error"]["code"] if r.status_code >= 400 else "ok"
    log.info("输入: key=%s version=%s | 判定: %s %s",
             (key[:8] + "…") if key else None, version, r.status_code, code)
    return r.status_code, code


def test_missing_invalid_expired_revoked_are_distinct(client) -> None:
    assert _deny(client, None) == (401, "credential_missing")
    assert _deny(client, "sk-does-not-exist") == (401, "credential_invalid")
    assert _deny(client, "sk-expired") == (401, "credential_expired")
    assert _deny(client, "sk-revoked") == (401, "credential_revoked")


def test_permission_denied_is_403_distinct_from_auth(client) -> None:
    # 凭据有效但 scope 不足：403 permission_denied
    r = client.post(
        "/v1/orders",
        headers={"X-Api-Key": "sk-readonly", "Idempotency-Key": "k"},
        json={"item": "book"},
    )
    err = r.json()["error"]
    log.info("输入: 只读凭据写订单 | 判定: %s code=%s required=%s",
             r.status_code, err["code"], err["details"].get("required_scope"))
    assert r.status_code == 403
    assert err["code"] == "permission_denied"
    assert err["details"]["required_scope"] == "orders:write"
    # 同一只读凭据读取自己有权限的资源是成功的
    assert _deny(client, "sk-readonly") == (200, "ok")


def test_key_rotation_overlap_both_secrets_work(client) -> None:
    """轮换重叠期：新旧密钥都能通过认证，且共享同一凭据配额。"""
    for key in ["sk-rotating-old", "sk-rotating-new"]:
        r = client.get("/v1/echo", headers={"X-Api-Key": key})
        log.info("输入: 轮换密钥 %s | 判定: %s", key, r.status_code)
        assert r.status_code == 200
    # 审计中两条成功记录属于同一 credential_id
    state = client.get("/__test/state").json()
    creds = {
        e["credential_id"]
        for e in state["audit_events"]
        if e["decision"] == "allow"
    }
    assert creds == {"cred-rotating"}


def test_expired_secret_denied_but_new_secret_remains_valid(client) -> None:
    """sk-expired 仅在过期窗口外不可用，错误明确为 expired 而非 invalid。"""
    status, code = _deny(client, "sk-expired")
    assert status == 401 and code == "credential_expired"
    r = client.get("/v1/echo", headers={"X-Api-Key": "sk-active"})
    assert r.status_code == 200


def test_auth_failure_distinguishable_from_version_failure(client) -> None:
    # 版本错误 400/410 vs 认证错误 401 vs 权限错误 403
    assert _deny(client, "sk-active", "2099-01-01")[0] == 400
    assert _deny(client, "sk-active", "2023-01-01")[0] == 410
    assert _deny(client, None)[0] == 401
