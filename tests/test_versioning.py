"""Version negotiation boundaries and cross-version semantic consistency."""
import logging

from tests.conftest import log_decision

log = logging.getLogger("tests.versioning")

H = {"X-Api-Key": "test-key-active"}


def test_default_version_is_deterministic(client):
    r = client.get("/widgets", headers=H)
    assert r.status_code == 200
    assert r.json()["api_version"] == "v2"
    assert r.headers["x-api-version"] == "v2"
    log_decision("default-version", r, header=None)


def test_explicit_supported_versions(client):
    for v in ("v2", "v3"):
        r = client.get("/widgets", headers={**H, "X-Api-Version": v})
        assert r.status_code == 200, r.text
        assert r.json()["api_version"] == v
        log_decision(f"supported-{v}", r, version=v)


def test_unknown_version_rejected_with_distinct_code(client):
    r = client.get("/widgets", headers={**H, "X-Api-Version": "v9"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "version_unknown"
    assert body["error"]["details"]["default_version"] == "v2"
    assert "v2" in body["error"]["details"]["supported"]
    # distinguishable from an auth failure (401)
    assert r.status_code != 401
    log_decision("unknown-version", r, version="v9")


def test_retired_version_rejected_with_retirement_info(client):
    r = client.get("/widgets", headers={**H, "X-Api-Version": "v0"})
    assert r.status_code == 410
    body = r.json()
    assert body["error"]["code"] == "version_retired"
    details = body["error"]["details"]
    assert details["successor"] == "v2"
    assert details["retirement"] == "2025-01-01"
    # distinguishable from unknown version and from permission failure
    assert body["error"]["code"] != "version_unknown"
    assert r.status_code != 403
    log_decision("retired-version", r, version="v0")


def test_deprecated_version_still_works_and_advertises_sunset(client):
    r = client.get("/widgets", headers={**H, "X-Api-Version": "v1"})
    assert r.status_code == 200
    assert r.headers["deprecation"] == "true"
    assert r.headers["x-successor-version"] == "v2"
    assert r.headers["sunset"]
    assert r.headers["x-retirement-date"] == "2027-06-01"
    log_decision("deprecated-version", r, version="v1")


def test_same_business_semantics_across_supported_versions(client):
    responses = {}
    for v in ("v1", "v2", "v3"):
        r = client.get("/widgets", headers={**H, "X-Api-Version": v})
        assert r.status_code == 200
        responses[v] = r.json()["data"]
    assert responses["v1"] == responses["v2"] == responses["v3"]
    log.info("cross-version payloads identical: %s", responses["v2"])


def test_version_failure_is_distinct_from_auth_failure(client):
    rv = client.get("/widgets", headers={"X-Api-Version": "v9"})
    ra = client.get("/widgets", headers={"X-Api-Key": "nope"})
    assert rv.json()["error"]["code"] == "version_unknown"
    assert ra.json()["error"]["code"] == "credential_invalid"
    log_decision("version-vs-auth(v)", rv, version="v9")
    log_decision("version-vs-auth(a)", ra, key="nope")


def test_versions_endpoint_documents_policy(client):
    r = client.get("/versions")
    assert r.status_code == 200
    policy = {v["version"]: v for v in r.json()["versions"]}
    assert policy["v0"]["status"] == "retired" and not policy["v0"]["still_available"]
    assert policy["v1"]["status"] == "deprecated"
    assert policy["v2"]["status"] == "supported"
    assert r.json()["default_version"] == "v2"
