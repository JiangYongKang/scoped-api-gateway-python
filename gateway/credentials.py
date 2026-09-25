"""Credential registry: validity windows, rotation overlap, and scopes.

Credentials are keyed by a hash of the secret; the raw secret is never stored
or logged. During key rotation two credentials for the same caller can
coexist, each enforced against its own ``valid_from``/``valid_until`` window.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

from gateway.errors import GatewayError
from gateway.models import Credential, CredentialState, RejectReason


def hash_secret(secret: str) -> str:
    # Demo-grade hashing (sha256). Real systems would use a salted KDF;
    # the point here is that plaintext secrets are never retained.
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class CredentialRegistry:
    def __init__(self) -> None:
        self._by_hash: dict[str, Credential] = {}

    def add(self, credential: Credential) -> None:
        if credential.secret_hash in self._by_hash:
            raise ValueError("duplicate credential secret")
        self._by_hash[credential.secret_hash] = credential

    def authenticate(self, secret: str, now: datetime) -> Credential:
        """Validate a presented secret and its time window / state.

        Raises GatewayError with a distinct code for:
        unknown secret, revoked credential, expired credential,
        credential used before its validity start.
        """
        presented = hash_secret(secret)
        matched: Credential | None = None
        for stored_hash, cred in self._by_hash.items():
            if hmac.compare_digest(presented, stored_hash):
                matched = cred
                break
        if matched is None:
            raise GatewayError(
                401, RejectReason.CREDENTIAL_INVALID,
                "Unknown API credential.",
                extra={"hint": "The presented API key does not exist."},
            )
        if matched.state is CredentialState.REVOKED:
            raise GatewayError(
                401, RejectReason.CREDENTIAL_REVOKED,
                "This API credential has been revoked.",
                extra={"key_id": matched.key_id},
            )
        if now < matched.valid_from:
            raise GatewayError(
                401, RejectReason.CREDENTIAL_INVALID,
                "API credential is not valid yet.",
                extra={"key_id": matched.key_id,
                       "valid_from": matched.valid_from.isoformat()},
            )
        if now >= matched.valid_until:
            raise GatewayError(
                401, RejectReason.CREDENTIAL_EXPIRED,
                "API credential has expired.",
                extra={"key_id": matched.key_id,
                       "valid_until": matched.valid_until.isoformat()},
            )
        if matched.state is CredentialState.EXPIRED:
            # Explicit state takes over after the window (defensive; the time
            # check above normally fires first).
            raise GatewayError(
                401, RejectReason.CREDENTIAL_EXPIRED,
                "API credential has expired.",
                extra={"key_id": matched.key_id},
            )
        return matched

    @staticmethod
    def has_scope(credential: Credential, scope: str) -> bool:
        return scope in credential.scopes
