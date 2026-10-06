"""Batch writes, shared route capacity, and self-service usage introspection."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import log_decision

log = logging.getLogger("tests.batch")

H = {"X-Api-Key": "test-key-active"}
H2 = {"X-Api-Key": "rot-new-secret"}
CREATE_LIMIT = 5       # per-credential quota on widgets:create
CREATE_CAPACITY = 8    # shared across all callers on widgets:create


def _batch(client, key, names, headers=None):
    return client.post(
        "/widgets/batch",
        headers={**(headers or H), "Idempotency-Key": key},
        json={"items": [{"name": n} for n in names]})


def _create(client, key, name, headers=None):
    return client.post(
        "/widgets",
        headers={**(headers or H), "Idempotency-Key": key},
        json={"name": name})


def _usage(client, headers=None, version=None):
    h = dict(headers or H)
    if version:
        h["X-Api-Version"] = version
    return client.get("/usage", headers=h)


def _create_usage(client, headers=None):
    body = _usage(client, headers).json()["data"]["routes"]["widgets:create"]
    return body["quota"], body["shared_capacity"]


def _widget_count(client):
    return len(client.get("/widgets", headers=H).json()["data"]["widgets"])


def test_batch_charges_per_record_written(client, services):
    r = _batch(client, "b-1", ["a", "b", "c"])
    assert r.status_code == 201
    assert r.json()["data"]["written"] == 3
    assert len(r.json()["data"]["widgets"]) == 3
    assert r.headers["x-ratelimit-limit"] == str(CREATE_LIMIT)
    assert r.headers["x-ratelimit-remaining"] == str(CREATE_LIMIT - 3)
    quota, cap = _create_usage(client)
    assert quota["used"] == 3 and quota["remaining"] == CREATE_LIMIT - 3
    assert cap["used"] == 3
    log_decision("batch-charge-3", r, amount=3)


def test_single_create_still_charges_exactly_one(client, services):
    r = _create(client, "s-1", "solo")
    assert r.status_code == 201
    quota, cap = _create_usage(client)
    assert quota["used"] == 1 and cap["used"] == 1
    log_decision("single-still-one", r)


def test_insufficient_quota_rejects_whole_batch_atomically(client, services):
    assert _batch(client, "b-1", ["a", "b", "c"]).status_code == 201
    before = _widget_count(client)
    # Only 2 units remain; a batch of 3 must be rejected as a whole.
    r = _batch(client, "b-2", ["d", "e", "f"])
    assert r.status_code == 429
    body = r.json()["error"]
    assert body["code"] == "quota_exceeded"
    assert body["details"]["requested"] == 3
    assert body["details"]["used"] == 3  # unchanged by the rejection
    assert int(r.headers["retry-after"]) >= 1
    # No partial writes, no charge-then-refund: state identical to before.
    assert _widget_count(client) == before
    quota, cap = _create_usage(client)
    assert quota["used"] == 3 and cap["used"] == 3
    # And the 2 units that do remain are still fully usable.
    ok = _batch(client, "b-3", ["d", "e"])
    assert ok.status_code == 201
    log_decision("batch-atomic-reject", r, remaining=2, requested=3)


def test_failed_batch_rolls_back_every_unit(client, services):
    # Record 2 is invalid: the whole batch fails *after* budget acquisition.
    r = client.post("/widgets/batch",
                    headers={**H, "Idempotency-Key": "bad-batch"},
                    json={"items": [{"name": "ok-1"}, {"name": ""},
                                    {"name": "ok-2"}]})
    assert r.status_code == 422
    # Every acquired unit returned: quota and capacity back to zero,
    # no widgets written, idempotency reservation released.
    quota, cap = _create_usage(client)
    assert quota["used"] == 0 and cap["used"] == 0
    assert _widget_count(client) == 1  # seed only
    retry = _batch(client, "bad-batch", ["ok-1", "ok-2"])
    assert retry.status_code == 201  # same key reusable after failure
    quota, _ = _create_usage(client)
    assert quota["used"] == 2
    log_decision("batch-rollback-then-retry", retry, failed_amount=3)


def test_batch_replay_has_no_extra_side_effect_or_charge(client, services):
    r1 = _batch(client, "idem-b", ["x", "y"])
    assert r1.status_code == 201
    first = r1.json()
    before_widgets = _widget_count(client)
    for _ in range(5):
        r2 = _batch(client, "idem-b", ["x", "y"])
        assert r2.status_code == 201
        assert r2.json()["idempotent_replay"] is True
        assert r2.json()["data"] == first["data"]
    assert _widget_count(client) == before_widgets
    quota, cap = _create_usage(client)
    assert quota["used"] == 2 and cap["used"] == 2  # charged once, not 6x
    log_decision("batch-replay", r2, key="idem-b")


def test_batch_same_key_different_payload_conflicts(client, services):
    assert _batch(client, "idem-c", ["x"]).status_code == 201
    r = _batch(client, "idem-c", ["x", "z"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
    quota, _ = _create_usage(client)
    assert quota["used"] == 1  # conflict never touches budget
    log_decision("batch-conflict", r, key="idem-c")


def test_shared_capacity_full_is_distinguishable_from_own_quota(client, services):
    # acct-active writes 5 (own quota full; capacity 5/8 used).
    assert _batch(client, "cap-a", ["a"] * 5).status_code == 201
    # rot-new writes 3 (capacity now 8/8 full, own quota 3/5 used).
    assert _batch(client, "cap-b", ["b"] * 3, H2).status_code == 201
    # rot-new still has 2 own units but the route is full -> capacity_exceeded.
    r_cap = _batch(client, "cap-b2", ["c"], H2)
    assert r_cap.status_code == 429
    assert r_cap.json()["error"]["code"] == "capacity_exceeded"
    assert r_cap.json()["error"]["details"]["scope"] == "shared_route_capacity"
    assert int(r_cap.headers["retry-after"]) >= 1
    # acct-active hits its *own* quota -> quota_exceeded, a different signal.
    r_own = _batch(client, "cap-a2", ["d"])
    assert r_own.status_code == 429
    assert r_own.json()["error"]["code"] == "quota_exceeded"
    assert r_own.json()["error"]["details"]["scope"] == "per_credential_quota"
    # Capacity rejection must not have cost rot-new any of its own budget.
    quota, cap = _create_usage(client, H2)
    assert quota["used"] == 3 and quota["remaining"] == 2
    assert cap["used"] == CREATE_CAPACITY
    log_decision("capacity-vs-quota", r_cap, caller="rot-new")
    log_decision("own-quota", r_own, caller="acct-active")


def test_shared_capacity_window_resets(client, services, clock):
    assert _batch(client, "win-a", ["a"] * 5).status_code == 201
    assert _batch(client, "win-b", ["b"] * 3, H2).status_code == 201
    assert _batch(client, "win-b2", ["c"], H2).status_code == 429
    clock.advance(seconds=3601)
    r = _batch(client, "win-b3", ["c", "d"], H2)
    assert r.status_code == 201
    log_decision("capacity-window-reset", r, t="+3601s")


def test_concurrent_batches_never_exceed_quota_or_capacity(client, services):
    # Two callers, batch size 2, 8 attempts each: per-caller limit 5,
    # shared capacity 8. Whatever the interleaving, the ceilings must hold.
    attempts = [(H, f"ra-{i}") for i in range(8)] + \
               [(H2, f"rb-{i}") for i in range(8)]
    results = [None] * len(attempts)
    barrier = threading.Barrier(len(attempts))

    def worker(i):
        headers, key = attempts[i]
        barrier.wait()
        results[i] = _batch(client, key, [f"n-{i}", f"m-{i}"], headers)

    with ThreadPoolExecutor(max_workers=len(attempts)) as pool:
        list(pool.map(worker, range(len(attempts))))

    created = [r for r in results if r.status_code == 201]
    rejected = [r for r in results if r.status_code == 429]
    written = sum(r.json()["data"]["written"] for r in created)
    log.info("concurrent batches: ok=%d rejected=%d written=%d",
             len(created), len(rejected), written)
    # Every admitted batch wrote its full batch (all-or-nothing) ...
    assert all(r.json()["data"]["written"] == 2 for r in created)
    # ... the shared ceiling was never crossed ...
    assert written <= CREATE_CAPACITY
    # ... and every rejection carried a retryable, distinguishable reason.
    assert rejected, "expected some rejections under this contention"
    for r in rejected:
        assert r.json()["error"]["code"] in ("quota_exceeded",
                                             "capacity_exceeded")
        assert int(r.headers["retry-after"]) >= 1
    # Side effects exactly match admitted units -- no partial writes.
    assert _widget_count(client) == 1 + written
    # Final water levels are consistent with what was admitted.
    q1, cap = _create_usage(client, H)
    q2, _ = _create_usage(client, H2)
    assert q1["used"] <= CREATE_LIMIT and q2["used"] <= CREATE_LIMIT
    assert q1["used"] + q2["used"] == written == cap["used"]


def test_usage_query_is_free_and_reports_own_window(client, services):
    assert _create(client, "u-1", "one").status_code == 201
    assert _batch(client, "u-2", ["two", "three"]).status_code == 201
    first = _usage(client).json()["data"]
    for _ in range(10):
        again = _usage(client)
        assert again.status_code == 200
    after = _usage(client).json()["data"]
    # Polling changed nothing: identical water levels, no quota consumed.
    assert after == first
    create = after["routes"]["widgets:create"]
    assert create["quota"]["used"] == 3
    assert create["quota"]["limit"] == CREATE_LIMIT
    assert create["quota"]["remaining"] == CREATE_LIMIT - 3
    assert "reset_at" in create["quota"]
    assert create["shared_capacity"]["used"] == 3
    assert create["shared_capacity"]["limit"] == CREATE_CAPACITY
    # And the budget really is untouched: the remaining 2 units still fit.
    assert _batch(client, "u-3", ["four", "five"]).status_code == 201
    log.info("usage polling x10 left quota untouched: %s", create["quota"])


def test_usage_shows_shared_water_level_across_callers(client, services):
    assert _batch(client, "sh-a", ["a"] * 2).status_code == 201
    assert _batch(client, "sh-b", ["b"] * 3, H2).status_code == 201
    q1, cap1 = _create_usage(client, H)
    q2, cap2 = _create_usage(client, H2)
    # Own budgets differ; the shared water level is the same for both.
    assert q1["used"] == 2 and q2["used"] == 3
    assert cap1 == cap2
    assert cap1["used"] == 5
    assert cap1["remaining"] == CREATE_CAPACITY - 5
    log.info("shared water level consistent across callers: %s", cap1)


def test_usage_requires_valid_credential(client, services):
    r = client.get("/usage")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_credential"
    r = client.get("/usage", headers={"X-Api-Key": "test-key-revoked"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "credential_revoked"
    log_decision("usage-auth", r)


def test_new_endpoints_behave_identically_across_versions(client, services):
    # Default version is used when the header is absent.
    r_default = _batch(client, "v-def", ["d1"])
    assert r_default.status_code == 201
    assert r_default.json()["api_version"] == "v2"
    # Same batch semantics on every available version.
    for version in ("v1", "v2", "v3"):
        hv = {**H, "X-Api-Version": version}
        r = client.post("/widgets/batch",
                        headers={**hv, "Idempotency-Key": f"v-{version}"},
                        json={"items": [{"name": f"n-{version}"}]})
        assert r.status_code == 201, r.text
        assert r.json()["api_version"] == version
        u = _usage(client, version=version)
        assert u.status_code == 200
        assert u.json()["api_version"] == version
        if version == "v1":
            assert u.headers["deprecation"] == "true"
    # Version negotiation failures still precede everything else.
    r = client.post("/widgets/batch",
                    headers={**H, "X-Api-Version": "v9",
                             "Idempotency-Key": "v-bad"},
                    json={"items": [{"name": "x"}]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_unknown"
    r = client.get("/usage", headers={**H, "X-Api-Version": "v0"})
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_retired"
    # Data payload of usage is version-independent apart from the envelope.
    d2 = _usage(client, version="v2").json()["data"]
    d3 = _usage(client, version="v3").json()["data"]
    assert d2 == d3
    log_decision("cross-version-batch", r_default)


def test_batch_requires_idempotency_key_and_non_empty_items(client, services):
    r = client.post("/widgets/batch", headers=H,
                    json={"items": [{"name": "x"}]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"
    for payload in ({}, {"items": []}, {"items": "nope"}):
        r = client.post("/widgets/batch",
                        headers={**H, "Idempotency-Key": "shape"},
                        json=payload)
        assert r.status_code == 400, payload
    quota, cap = _create_usage(client)
    assert quota["used"] == 0 and cap["used"] == 0
    log_decision("batch-shape-400", r)
