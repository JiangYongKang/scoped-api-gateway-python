"""Per-credential per-route quota under concurrency and retry/failure rules."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import log_decision

log = logging.getLogger("tests.quota")

H = {"X-Api-Key": "test-key-active"}
CREATE_LIMIT = 5


def _create(client, i):
    return client.post(
        "/widgets",
        headers={**H, "Idempotency-Key": f"race-{i}"},
        json={"name": f"n-{i}"})


def test_concurrent_quota_race_admits_exactly_the_limit(client, services):
    n = 60
    results = [None] * n
    start = threading.Barrier(n)

    def worker(i):
        start.wait()
        results[i] = _create(client, i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    statuses = [r.status_code for r in results]
    allowed = [s for s in statuses if s == 201]
    rejected = [r for r in results if r.status_code == 429]
    log.info("concurrent inputs N=%d -> admitted=%d rejected=%d",
             n, len(allowed), len(rejected))
    assert len(allowed) == CREATE_LIMIT
    assert len(rejected) == n - CREATE_LIMIT
    body = rejected[0].json()
    assert body["error"]["code"] == "quota_exceeded"
    assert int(rejected[0].headers["retry-after"]) >= 1
    details = body["error"]["details"]
    assert details["used"] == CREATE_LIMIT and details["limit"] == CREATE_LIMIT
    # Exactly LIMIT side effects, no overshoot.
    listed = client.get("/widgets", headers=H).json()["data"]["widgets"]
    assert len(listed) == 1 + CREATE_LIMIT  # seed w-1 + creates
    log_decision("quota-race-429", rejected[0], n=n)


def test_rejected_attempts_do_not_consume_quota(client, services):
    # Scope failures (403) hit the pipeline before quota acquisition.
    for _ in range(10):
        r = client.post("/widgets",
                        headers={"X-Api-Key": "test-key-readonly",
                                 "Idempotency-Key": "x"},
                        json={"name": "x"})
        assert r.status_code == 403
    # Idempotency replays/conflicts must not consume quota either:
    r = _create(client, 1)
    assert r.status_code == 201
    for _ in range(CREATE_LIMIT * 3):
        replay = client.post(
            "/widgets", headers={**H, "Idempotency-Key": "race-1"},
            json={"name": "n-1"})
        assert replay.status_code == 201
        assert replay.json()["idempotent_replay"] is True
    # Conflict attempts on that key also must not consume quota.
    for _ in range(5):
        conflict = client.post(
            "/widgets", headers={**H, "Idempotency-Key": "race-1"},
            json={"name": "different"})
        assert conflict.status_code == 409
    # After 1 real create + replays + conflicts, 4 more real creates fit.
    for i in range(2, 6):
        assert _create(client, i).status_code == 201
    assert _create(client, 6).status_code == 429
    log.info("no double-count: replays/conflicts left exactly 4 slots")


def test_failed_business_handling_releases_quota(client, services):
    # Invalid payloads fail after reservation but before completion; the
    # route rolls quota back, so retries are not punished.
    for i in range(CREATE_LIMIT + 3):
        r = client.post(
            "/widgets", headers={**H, "Idempotency-Key": f"bad-{i}"},
            json={"name": ""})
        assert r.status_code == 422, r.text
    ok = _create(client, 99)
    assert ok.status_code == 201
    log_decision("failed-then-retry", ok, failed_attempts=CREATE_LIMIT + 3)


def test_quota_is_per_credential_and_per_route(client, services):
    # read-only credential has its own bucket on list route
    for _ in range(3):
        assert client.get(
            "/widgets", headers={"X-Api-Key": "test-key-readonly"}
        ).status_code == 200
    # active account exhausted on create but list has independent budget
    for i in range(CREATE_LIMIT):
        assert _create(client, 100 + i).status_code == 201
    assert _create(client, 200).status_code == 429
    assert client.get("/widgets", headers=H).status_code == 200
    log.info("quota buckets are independent by (credential, route)")


def test_quota_window_resets(client, services, clock):
    for i in range(CREATE_LIMIT):
        assert _create(client, 300 + i).status_code == 201
    assert _create(client, 400).status_code == 429
    clock.advance(seconds=3601)
    r = _create(client, 401)
    assert r.status_code == 201
    log_decision("window-reset", r, t="+3601s")
