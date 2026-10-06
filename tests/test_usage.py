"""Usage self-service: read-only, version-consistent, no state mutation."""
import logging

from tests.conftest import log_decision

log = logging.getLogger("tests.usage")

H = {"X-Api-Key": "test-key-active"}
CREATE_LIMIT = 5


def _create(client, key):
    return client.post("/widgets",
                       headers={**H, "Idempotency-Key": key},
                       json={"name": f"n-{key}"})


def test_usage_reports_own_window_and_shared_water_level(client):
    for i in range(2):
        assert _create(client, f"u-{i}").status_code == 201
    r = client.get("/usage", headers=H)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["key_id"] == "acct-active"
    assert data["window_seconds"] == 3600
    create = data["routes"]["widgets:create"]
    assert create["used"] == 2
    assert create["limit"] == CREATE_LIMIT
    assert create["remaining"] == CREATE_LIMIT - 2
    assert create["reset_at"]
    shared = create["shared"]
    assert shared["used"] == 2
    assert shared["limit"] == 50
    assert shared["remaining"] == 48
    # read routes are reported too
    assert "widgets:list" in data["routes"]
    log_decision("usage-view", r, after_creates=2)


def test_usage_query_consumes_no_quota_and_mutates_nothing(client, services):
    for _ in range(10):
        r = client.get("/usage", headers=H)
        assert r.status_code == 200
    data = r.json()["data"]["routes"]
    assert data["widgets:create"]["used"] == 0
    assert data["widgets:create"]["shared"]["used"] == 0
    assert data["widgets:list"]["used"] == 0
    # full quota still available afterwards
    for i in range(CREATE_LIMIT):
        assert _create(client, f"q-{i}").status_code == 201
    assert _create(client, "q-over").status_code == 429
    log.info("10 usage queries consumed nothing; full limit of %d intact",
             CREATE_LIMIT)


def test_usage_is_consistent_across_versions(client):
    assert _create(client, "v-0").status_code == 201
    bodies = {}
    for version in (None, "v1", "v2", "v3"):
        headers = dict(H)
        if version:
            headers["X-Api-Version"] = version
        r = client.get("/usage", headers=headers)
        assert r.status_code == 200
        body = r.json()
        expected = version or "v2"  # deterministic default
        assert body["api_version"] == expected
        bodies[version] = (body["data"], r.headers)
        if version == "v1":
            assert r.headers["deprecation"] == "true"
    # identical data on every available version
    assert len({str(b) for b, _ in bodies.values()}) == 1
    log.info("usage data identical across default/v1/v2/v3")


def test_usage_version_and_auth_failures_unchanged(client):
    # retired / unknown versions still rejected exactly as before
    r = client.get("/usage", headers={**H, "X-Api-Version": "v0"})
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"
    r = client.get("/usage", headers={**H, "X-Api-Version": "v9"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_unknown"
    # missing / invalid credentials still 401
    assert client.get("/usage").status_code == 401
    r = client.get("/usage", headers={"X-Api-Key": "nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "credential_invalid"
    log_decision("usage-version-auth", r)


def test_usage_available_to_readonly_credential(client):
    r = client.get("/usage", headers={"X-Api-Key": "test-key-readonly"})
    assert r.status_code == 200
    assert r.json()["data"]["key_id"] == "acct-readonly"
    log_decision("usage-readonly", r)


def test_batch_endpoint_behaves_the_same_on_all_versions(client):
    for version in ("v1", "v2", "v3"):
        r = client.post(
            "/widgets/batch",
            headers={**H, "Idempotency-Key": f"ver-{version}",
                     "X-Api-Version": version},
            json={"items": [{"name": f"w-{version}"}]})
        assert r.status_code == 201
        assert r.json()["api_version"] == version
        assert r.json()["data"]["count"] == 1
    # all three batches landed on the same per-credential bucket
    usage = client.get("/usage", headers=H).json()["data"]["routes"]
    assert usage["widgets:create"]["used"] == 3
    log.info("batch writes consistent across v1/v2/v3, unified accounting")
