"""Batch writes: per-record billing, all-or-nothing admission, rollback."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import log_decision

log = logging.getLogger("tests.batch")

H = {"X-Api-Key": "test-key-active"}
CREATE_LIMIT = 5


def _batch(client, key, names, headers=None):
    return client.post(
        "/widgets/batch",
        headers={**H, "Idempotency-Key": key, **(headers or {})},
        json={"items": [{"name": n} for n in names]})


def _single(client, key, name="s"):
    return client.post("/widgets",
                       headers={**H, "Idempotency-Key": key},
                       json={"name": name})


def _widget_count(client):
    return len(client.get("/widgets", headers=H).json()["data"]["widgets"])


def test_batch_charges_per_record_and_single_still_charges_one(client):
    r = _batch(client, "b-1", ["a", "b", "c"])
    assert r.status_code == 201
    assert r.json()["data"]["count"] == 3
    assert r.headers["x-ratelimit-limit"] == str(CREATE_LIMIT)
    assert r.headers["x-ratelimit-remaining"] == str(CREATE_LIMIT - 3)
    log_decision("batch-3", r, batch_size=3)

    s = _single(client, "s-1")
    assert s.status_code == 201
    # single create still bills exactly one unit on the same bucket
    assert s.headers["x-ratelimit-remaining"] == str(CREATE_LIMIT - 4)
    log_decision("single-after-batch", s)


def test_batch_rejected_wholesale_when_quota_insufficient(client):
    assert _batch(client, "b-1", ["a", "b", "c"]).status_code == 201  # used 3
    before = _widget_count(client)
    r = _batch(client, "b-2", ["d", "e", "f"])  # needs 3, only 2 left
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "quota_exceeded"
    assert body["error"]["details"]["requested"] == 3
    assert body["error"]["details"]["used"] == 3
    assert int(r.headers["retry-after"]) >= 1
    log_decision("batch-wholesale-reject", r, batch_size=3, remaining=2)
    # nothing written, nothing held: widget count and usage unchanged
    assert _widget_count(client) == before
    usage = client.get("/usage", headers=H).json()["data"]["routes"]
    assert usage["widgets:create"]["used"] == 3
    # the remaining 2 units still fit a smaller batch exactly
    assert _batch(client, "b-3", ["d", "e"]).status_code == 201
    assert _single(client, "s-9").status_code == 429


def test_failed_batch_rolls_back_every_held_unit(client):
    before = _widget_count(client)
    r = _batch(client, "b-bad", ["ok-1", "", "ok-2"])
    assert r.status_code == 422
    log_decision("batch-business-failure", r, batch_size=3)
    # no partial writes
    assert _widget_count(client) == before
    # all 3 quota units returned: the full limit is available again
    for i in range(CREATE_LIMIT):
        assert _single(client, f"after-{i}").status_code == 201
    assert _single(client, "after-overflow").status_code == 429
    log.info("rollback verified: full limit of %d reusable after 422",
             CREATE_LIMIT)


def test_batch_replay_has_no_extra_side_effect_or_charge(client):
    r1 = _batch(client, "b-rep", ["x", "y"])
    assert r1.status_code == 201
    first = r1.json()
    before = _widget_count(client)
    for _ in range(3):
        r2 = _batch(client, "b-rep", ["x", "y"])
        assert r2.status_code == 201
        assert r2.headers["idempotent-replay"] == "true"
        assert r2.json()["idempotent_replay"] is True
        assert r2.json()["data"] == first["data"]
    assert _widget_count(client) == before
    usage = client.get("/usage", headers=H).json()["data"]["routes"]
    assert usage["widgets:create"]["used"] == 2  # charged once, for 2 records
    log_decision("batch-replay", r2, key="b-rep")


def test_batch_same_key_different_payload_is_conflict(client):
    assert _batch(client, "b-cf", ["a"]).status_code == 201
    r = _batch(client, "b-cf", ["a", "b"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
    log_decision("batch-conflict", r, key="b-cf")


def test_concurrent_batch_quota_race_never_overshoots(client):
    # 10 concurrent batches of 2 against a limit of 5: only 2 batches
    # (4 units) can be admitted; a third would need 6 > 5.
    n, size = 10, 2
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        results[i] = _batch(client, f"race-{i}", [f"n{i}-a", f"n{i}-b"])

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    admitted = [r for r in results if r.status_code == 201]
    rejected = [r for r in results if r.status_code == 429]
    created = sum(r.json()["data"]["count"] for r in admitted)
    log.info("concurrent batch race: admitted=%d rejected=%d created=%d",
             len(admitted), len(rejected), created)
    assert len(admitted) == 2 and created == 4
    assert len(rejected) == n - 2
    assert all(r.json()["error"]["code"] == "quota_exceeded"
               for r in rejected)
    # no partial batches, no dirty accounting
    assert _widget_count(client) == 1 + created  # seed w-1 + created
    usage = client.get("/usage", headers=H).json()["data"]["routes"]
    assert usage["widgets:create"]["used"] == created
    assert size > 0  # batch size documented in inputs above
