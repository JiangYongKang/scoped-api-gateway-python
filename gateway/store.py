"""Deterministic in-memory seed data for local runs and tests.

Seeded credentials (all relative to the provided clock time):

============================== ================= =================================
 secret                         key_id            state / notes
============================== ================= =================================
 test-key-active                acct-active       read+write
 rot-old-secret                 acct-rot-old      rotation: expires soon
 rot-new-secret                 acct-rot-new      rotation: already valid
 test-key-expired               acct-expired      past valid_until
 test-key-revoked               acct-revoked      explicitly revoked
 test-key-readonly              acct-readonly     read scope only
============================== ================= =================================
"""
from __future__ import annotations

from datetime import datetime, timedelta

from gateway.credentials import CredentialRegistry, hash_secret
from gateway.models import Credential, CredentialState

# Route keys double as scope names and quota buckets.
ROUTE_WIDGETS_LIST = "widgets:list"
ROUTE_WIDGET_DETAIL = "widgets:detail"
ROUTE_WIDGETS_CREATE = "widgets:create"

DEFAULT_LIMITS = {
    ROUTE_WIDGETS_LIST: 100,
    ROUTE_WIDGET_DETAIL: 100,
    ROUTE_WIDGETS_CREATE: 5,
}


def build_registry(now: datetime) -> CredentialRegistry:
    reg = CredentialRegistry()

    def cred(secret: str, key_id: str, state: CredentialState,
             valid_from: datetime, valid_until: datetime, scopes,
             limits=None) -> Credential:
        return Credential(
            key_id=key_id, secret_hash=hash_secret(secret), state=state,
            valid_from=valid_from, valid_until=valid_until,
            scopes=frozenset(scopes), quota_limits=dict(limits or DEFAULT_LIMITS),
        )

    reg.add(cred(
        "test-key-active", "acct-active", CredentialState.ACTIVE,
        now - timedelta(days=30), now + timedelta(days=30),
        {ROUTE_WIDGETS_LIST, ROUTE_WIDGET_DETAIL, ROUTE_WIDGETS_CREATE},
    ))
    # Rotation window: old still valid for 2h, new became valid 1h ago.
    reg.add(cred(
        "rot-old-secret", "acct-rot-old", CredentialState.ACTIVE,
        now - timedelta(days=30), now + timedelta(hours=2),
        {ROUTE_WIDGETS_LIST, ROUTE_WIDGET_DETAIL, ROUTE_WIDGETS_CREATE},
    ))
    reg.add(cred(
        "rot-new-secret", "acct-rot-new", CredentialState.ACTIVE,
        now - timedelta(hours=1), now + timedelta(days=30),
        {ROUTE_WIDGETS_LIST, ROUTE_WIDGET_DETAIL, ROUTE_WIDGETS_CREATE},
    ))
    reg.add(cred(
        "test-key-expired", "acct-expired", CredentialState.EXPIRED,
        now - timedelta(days=30), now - timedelta(days=1),
        {ROUTE_WIDGETS_LIST},
    ))
    reg.add(cred(
        "test-key-revoked", "acct-revoked", CredentialState.REVOKED,
        now - timedelta(days=30), now + timedelta(days=30),
        {ROUTE_WIDGETS_LIST, ROUTE_WIDGET_DETAIL, ROUTE_WIDGETS_CREATE},
    ))
    reg.add(cred(
        "test-key-readonly", "acct-readonly", CredentialState.ACTIVE,
        now - timedelta(days=30), now + timedelta(days=30),
        {ROUTE_WIDGETS_LIST, ROUTE_WIDGET_DETAIL},
    ))
    return reg


def build_widgets() -> dict[str, dict]:
    return {
        "w-1": {"id": "w-1", "name": "alpha widget", "versioned_payload": "same-on-every-version"},
    }
