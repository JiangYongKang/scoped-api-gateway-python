"""Idempotent writes: replay identity, payload conflict, concurrency, audit."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import log_decision

log = logging.getLogger("tests.idempotency")

H = {"X-Api-Key": "test-key-active"}


def _post(client, key, payload, extra_headers=None):
    return client.post(
        "/widgets",
        headers={**H, "Idempotency-Key": key, **(extra_headers or {})},
        json=payload)


def test_replay_returns_identical_result_without_new_side_effect(client, services):
    r1 = _post(client, "order-1", {"name": "first"})
    assert r1.status_code == 201
    first_body = r1.json()
    assert "idempotent_replay" not in first_body
    r2 = _post(client, "order-1", {"name": "first"})
    assert r2.status_code == 201
    assert r2.headers["idempotent-replay"] == "true"
    second = r2.json()
    assert second["idempotent_replay"] is True
    # same business result as the first
    assert second["data"] == first_body["data"]
    widgets = client.get("/widgets", headers=H).json()["data"]["widgets"]
    names = [w["name"] for w in widgets if w["id"] == first_body["data"]["widget"]["id"]]
    assert names == ["first"]
    log_decision("replay", r2, key="order-1")


def test_same_key_different_payload_is_conflict(client, services):
    r1 = _post(client, "order-2", {"name": "aaa"})
    assert r1.status_code == 201
    r2 = _post(client, "order-2", {"name": "bbb"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"
    # conflict must not create a widget nor overwrite the stored result
    listed = client.get("/widgets", headers=H).json()["data"]["widgets"]
    assert all(w["name"] != "bbb" for w in listed)
    r3 = _post(client, "order-2", {"name": "aaa"})
    assert r3.status_code == 201 and r3.json()["data"]["widget"]["name"] == "aaa"
    log_decision("payload-conflict", r2, key="order-2")


def test_concurrent_same_key_produces_one_side_effect(client, services):
    n = 25
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        results[i] = _post(client, "hot-key", {"name": "same"})

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))
    created = [r for r in results if r.status_code == 201
               and "idempotent_replay" not in r.json()]
    replays = [r for r in results if r.status_code == 201
               and r.json().get("idempotent_replay")]
    assert len(created) == 1, [r.status_code for r in results]
    assert len(replays) == n - 1
    ids = {r.json()["data"]["widget"]["id"] for r in results}
    assert ids == {created[0].json()["data"]["widget"]["id"]}
    log.info("concurrent same key: 1 creation + %d replays, single id %s",
             len(replays), ids)


def test_same_key_different_caller_is_conflict(client, services):
    r1 = _post(client, "order-3", {"name": "x"})
    assert r1.status_code == 201
    r2 = client.post(
        "/widgets",
        headers={"X-Api-Key": "rot-new-secret", "Idempotency-Key": "order-3"},
        json={"name": "x"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"
    log_decision("cross-caller-conflict", r2, key="order-3")


def test_missing_idempotency_key_rejected(client, services):
    r = client.post("/widgets", headers=H, json={"name": "no-key"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"
    log_decision("missing-key", r)


def test_idempotency_decisions_are_audited_with_caller(client, services):
    _post(client, "audit-1", {"name": "a"})
    _post(client, "audit-1", {"name": "a"})  # replay
    _post(client, "audit-1", {"name": "b"})  # conflict
    entries = [e for e in services["audit"].entries()
               if e.route == "widgets:create"]
    by_decision = {}
    for e in entries:
        by_decision.setdefault(e.decision, []).append(e)
    # exactly one quota-consuming allow, one replay decision, one conflict
    assert len(by_decision["allow"]) == 1
    assert by_decision["allow"][0].quota_before == 0
    assert by_decision["allow"][0].quota_after == 1
    assert [e.reason for e in by_decision["replay"]] == ["idempotent_replay"]
    assert [e.reason for e in by_decision["reject"]] == ["idempotency_conflict"]
    # all three auditable decisions correlate to the same caller
    assert {e.key_id for e in entries} == {"acct-active"}
    # replays/conflicts never touched quota: net consumption is exactly 1,
    # so 4 more real creates still fit (limit = 5)
    probe = services["quota"].try_acquire(
        "acct-active", "widgets:create", 5, services["clock"].now())
    assert probe.used == 2  # 1 consumed + this probe
    services["quota"].rollback("acct-active", "widgets:create")
    services["audit"].assert_no_secret("test-key-active")
    log.info("idempotency audit trail verified: %s",
             [(e.decision, e.reason, e.quota_before, e.quota_after)
              for e in entries])
