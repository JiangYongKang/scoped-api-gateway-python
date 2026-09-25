"""版本协商边界测试。

覆盖：默认版本确定性、显式选择、未知版本(400 version_unknown)、
已下线版本(410 version_retired)、弃用版本头、与认证失败可区分、
不同受支持版本下业务语义一致。
"""

from __future__ import annotations

import logging

log = logging.getLogger("test.versions")

H = {"X-Api-Key": "sk-active"}


def test_default_version_is_deterministic(client) -> None:
    """未指定版本时固定落到默认版本，不隐式升级。"""
    r = client.get("/v1/echo", headers=H)
    log.info("输入: 无 X-Api-Version | 判定: 200 version=%s body=%s",
             r.headers.get("x-api-version"), r.json())
    assert r.status_code == 200
    assert r.headers["x-api-version"] == "2024-01-01"
    assert r.json() == {"semantic": "echo-v1", "echo": "hello"}


def test_explicit_supported_versions_same_semantics(client) -> None:
    """同一业务语义在不同受支持版本下结果一致（版本差异仅体现在头）。"""
    bodies = {}
    for v in ["2024-01-01", "2026-01-01"]:
        r = client.get("/v1/echo", headers={**H, "X-Api-Version": v})
        log.info("输入: version=%s | 判定: %s body=%s", v, r.status_code, r.json())
        assert r.status_code == 200
        assert r.headers["x-api-version"] == v
        bodies[v] = r.json()
    assert bodies["2024-01-01"] == bodies["2026-01-01"]


def test_deprecated_version_carries_deprecation_headers(client) -> None:
    """弃用版本仍可用，但必须返回可识别的弃用与下线信息。"""
    r = client.get("/v1/echo", headers={**H, "X-Api-Version": "2025-01-01"})
    log.info("输入: version=2025-01-01 | 判定: %s Deprecation=%s Sunset=%s",
             r.status_code, r.headers.get("deprecation"), r.headers.get("sunset"))
    assert r.status_code == 200
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == "Thu, 01 Oct 2026 00:00:00 GMT"
    assert "successor-version" in r.headers["link"]
    assert "2026-01-01" in r.headers["link"]


def test_unknown_version_rejected_distinct_from_auth(client) -> None:
    """未知版本 -> 400 version_unknown；与 401 认证失败可区分。"""
    r = client.get("/v1/echo", headers={**H, "X-Api-Version": "2099-01-01"})
    err = r.json()["error"]
    log.info("输入: version=2099-01-01 | 判定: %s code=%s details=%s",
             r.status_code, err["code"], err["details"])
    assert r.status_code == 400
    assert err["code"] == "version_unknown"
    assert err["details"]["default_version"] == "2024-01-01"
    assert "2026-01-01" in err["details"]["supported_versions"]

    # 对照：无凭据的认证失败是另一个状态码与错误码
    r_auth = client.get("/v1/echo")
    log.info("输入: 无凭据 | 判定: %s code=%s",
             r_auth.status_code, r_auth.json()["error"]["code"])
    assert r_auth.status_code == 401
    assert r_auth.json()["error"]["code"] == "credential_missing"


def test_retired_version_410_with_successor(client) -> None:
    """已下线版本 -> 410 version_retired，并给出继任版本。"""
    r = client.get("/v1/echo", headers={**H, "X-Api-Version": "2023-01-01"})
    err = r.json()["error"]
    log.info("输入: version=2023-01-01 | 判定: %s code=%s successor=%s",
             r.status_code, err["code"], err["details"].get("successor"))
    assert r.status_code == 410
    assert err["code"] == "version_retired"
    assert err["details"]["successor"] == "2024-01-01"
    # 版本判定先于业务：下线版本即使无凭据也报版本错误（两者可区分，不混为一谈）
    r2 = client.get("/v1/echo",
                    headers={"X-Api-Version": "2023-01-01"})
    assert r2.status_code == 410
    assert r2.json()["error"]["code"] == "version_retired"


def test_blank_version_header_falls_back_to_default(client) -> None:
    r = client.get("/v1/echo", headers={**H, "X-Api-Version": ""})
    log.info("输入: 空 X-Api-Version | 判定: %s version=%s",
             r.status_code, r.headers.get("x-api-version"))
    assert r.status_code == 200
    assert r.headers["x-api-version"] == "2024-01-01"
