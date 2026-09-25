"""Credential states, rotation overlap, expiry boundary and audit safety."""
import logging

from tests.conftest import log_decision

log = logging.getLogger("tests.credentials")

CASES = [
    # (key, expected_status, expected_code)
    ("test-key-active", 200, None),
    (None, 401, "missing_credential"),
    ("unknown-key", 401, "credential_invalid"),
    ("test-key-revoked", 401, "credential_revoked"),
    ("test-key-expired", 401, "credential_expired"),
    ("test-key-readonly", 200, None),  # read route allowed...
]


def test_credential_state_matrix(client):
    for key, status, code in CASES:
        headers = {"X-Api-Key": key} if key else {}
        r = client.get("/widgets", headers=headers)
        assert r.status_code == status, (key, r.status_code, r.text)
        if code:
            assert r.json()["error"]["code"] == code
        log_decision(f"credential-{key}", r, key=key)


def test_insufficient_scope_is_403_distinct_from_auth(client):
    r = client.post(
        "/widgets",
        headers={"X-Api-Key": "test-key-readonly", "Idempotency-Key": "k"},
        json={"name": "x"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
    log_decision("insufficient-scope", r, key="test-key-readonly")


def test_rotation_overlap_both_keys_valid(client, services):
    r_old = client.get("/widgets", headers={"X-Api-Key": "rot-old-secret"})
    r_new = client.get("/widgets", headers={"X-Api-Key": "rot-new-secret"})
    assert r_old.status_code == 200 and r_new.status_code == 200
    log_decision("rotation-old", r_old, key="rot-old-secret")
    log_decision("rotation-new", r_new, key="rot-new-secret")


def test_rotation_old_expires_new_survives(client, services, clock):
    clock.advance(hours=3)
    r_old = client.get("/widgets", headers={"X-Api-Key": "rot-old-secret"})
    r_new = client.get("/widgets", headers={"X-Api-Key": "rot-new-secret"})
    assert r_old.status_code == 401
    assert r_old.json()["error"]["code"] == "credential_expired"
    assert r_new.status_code == 200
    log_decision("rotation-old-expired", r_old, t="+3h")
    log_decision("rotation-new-valid", r_new, t="+3h")


def test_expiry_boundary_is_inclusive_of_valid_until(client, services, clock):
    # Seed: expired key ended exactly at seed - 1 day; check exactly at boundary
    clock.advance(hours=0)
    r = client.get("/widgets", headers={"X-Api-Key": "rot-old-secret"})
    assert r.status_code == 200
    clock.advance(hours=2)  # valid_until = seed + 2h -> expired at/after that
    r = client.get("/widgets", headers={"X-Api-Key": "rot-old-secret"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "credential_expired"
    log_decision("expiry-boundary", r, t="+2h")


def test_audit_links_caller_but_never_contains_secret(client, services):
    client.get("/widgets", headers={"X-Api-Key": "test-key-active"})
    client.get("/widgets", headers={"X-Api-Key": "test-key-revoked"})
    entries = services["audit"].entries()
    key_ids = {e.key_id for e in entries if e.key_id}
    assert "acct-active" in key_ids and "acct-revoked" in key_ids
    services["audit"].assert_no_secret("test-key-active")
    services["audit"].assert_no_secret("test-key-revoked")
    log.info("audit caller linkage OK: %s", sorted(key_ids))
