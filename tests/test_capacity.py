"""Shared per-route capacity ceiling: strict under concurrency, distinct
from per-credential quota, rolled back on business failure."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.deps import build_services
from gateway.store import (ROUTE_WIDGET_DETAIL, ROUTE_WIDGETS_CREATE,
                           ROUTE_WIDGETS_LIST)
from tests.conftest import log_decision

log = logging.getLogger("tests.capacity")

H1 = {"X-Api-Key": "test-key-active"}    # own create quota: 5
H2 = {"X-Api-Key": "rot-new-secret"}     # own create quota: 5
SHARED_CAP = 7                            # < 5 + 5, so the ceiling binds


@pytest.fixture
def cap_env(clock):
    services = build_services(clock, shared_capacity={
        ROUTE_WIDGETS_CREATE: SHARED_CAP,
        ROUTE_WIDGETS_LIST: 1000,
        ROUTE_WIDGET_DETAIL: 1000,
    })
    return TestClient(create_app(services)), services


def _create(client, headers, key):
    return client.post("/widgets",
                       headers={**headers, "Idempotency-Key": key},
                       json={"name": f"n-{key}"})


def test_shared_capacity_strict_under_concurrency(cap_env):
    client, services = cap_env
    # Two callers, each with their own quota of 5, hammer the same route:
    # combined demand 10 > shared ceiling 7.
    n = 20
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        results[i] = _create(client, H1 if i % 2 == 0 else H2, f"c-{i}")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    admitted = [r for r in results if r.status_code == 201]
    cap_rejected = [r for r in results
                    if r.status_code == 429
                    and r.json()["error"]["code"] == "capacity_exceeded"]
    log.info("shared-cap race: admitted=%d capacity_rejected=%d",
             len(admitted), len(cap_rejected))
    # the ceiling is never exceeded, no matter the interleaving
    assert len(admitted) == SHARED_CAP
    assert len(cap_rejected) >= 1
    widgets = client.get("/widgets", headers=H1).json()["data"]["widgets"]
    assert len(widgets) == 1 + SHARED_CAP
    # both callers can see the same saturated water level
    for h in (H1, H2):
        usage = client.get("/usage", headers=h).json()["data"]["routes"]
        shared = usage["widgets:create"]["shared"]
        assert shared["used"] == SHARED_CAP
        assert shared["limit"] == SHARED_CAP
        assert shared["remaining"] == 0
    log_decision("capacity-race-429", cap_rejected[0], n=n,
                 shared_cap=SHARED_CAP)


def test_capacity_error_is_distinguishable_from_quota_error(cap_env):
    client, _ = cap_env
    # Caller 1 fills the shared ceiling almost alone (own quota 5).
    for i in range(5):
        assert _create(client, H1, f"a-{i}").status_code == 201
    # Caller 1's OWN quota is now exhausted -> quota_exceeded.
    r_quota = _create(client, H1, "a-over")
    assert r_quota.status_code == 429
    assert r_quota.json()["error"]["code"] == "quota_exceeded"
    assert int(r_quota.headers["retry-after"]) >= 1
    # Caller 2 still has own quota; 2 more creates fill the shared ceiling.
    assert _create(client, H2, "b-0").status_code == 201
    assert _create(client, H2, "b-1").status_code == 201
    # Now the route is full for everyone -> capacity_exceeded.
    r_cap = _create(client, H2, "b-2")
    assert r_cap.status_code == 429
    body = r_cap.json()
    assert body["error"]["code"] == "capacity_exceeded"
    details = body["error"]["details"]
    assert details["shared_used"] == SHARED_CAP
    assert details["shared_limit"] == SHARED_CAP
    assert details["retry_after_seconds"] >= 1
    assert int(r_cap.headers["retry-after"]) >= 1
    log_decision("quota-vs-capacity", r_cap, caller="acct-rot-new")
    log_decision("quota-vs-capacity", r_quota, caller="acct-active")
    # capacity rejection held nothing of caller 2's own quota
    usage = client.get("/usage", headers=H2).json()["data"]["routes"]
    assert usage["widgets:create"]["used"] == 2


def test_failed_batch_returns_shared_capacity_units(cap_env):
    client, _ = cap_env
    r = client.post("/widgets/batch",
                    headers={**H1, "Idempotency-Key": "cap-bad"},
                    json={"items": [{"name": "x"}, {"name": ""}]})
    assert r.status_code == 422
    usage = client.get("/usage", headers=H1).json()["data"]["routes"]
    create = usage["widgets:create"]
    assert create["used"] == 0
    assert create["shared"]["used"] == 0
    log.info("business failure returned all shared units: %s", create)


def test_batch_counts_against_shared_capacity(cap_env):
    client, _ = cap_env
    # batch of 4 (caller 1) + batch of 4 (caller 2) = 8 > cap 7:
    # the second batch must be refused wholesale.
    r1 = client.post("/widgets/batch",
                     headers={**H1, "Idempotency-Key": "cap-b1"},
                     json={"items": [{"name": n} for n in "abcd"]})
    assert r1.status_code == 201
    r2 = client.post("/widgets/batch",
                     headers={**H2, "Idempotency-Key": "cap-b2"},
                     json={"items": [{"name": n} for n in "efgh"]})
    assert r2.status_code == 429
    assert r2.json()["error"]["code"] == "capacity_exceeded"
    # caller 2's own quota untouched by the capacity rejection
    usage2 = client.get("/usage", headers=H2).json()["data"]["routes"]
    assert usage2["widgets:create"]["used"] == 0
    # a smaller batch that fits the remaining 3 shared units succeeds
    r3 = client.post("/widgets/batch",
                     headers={**H2, "Idempotency-Key": "cap-b3"},
                     json={"items": [{"name": n} for n in "efg"]})
    assert r3.status_code == 201
    usage1 = client.get("/usage", headers=H1).json()["data"]["routes"]
    assert usage1["widgets:create"]["shared"]["used"] == SHARED_CAP
    log_decision("batch-vs-shared-cap", r2, batch_size=4,
                 shared_remaining=SHARED_CAP - 4)
