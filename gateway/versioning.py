"""API version negotiation.

Callers select a version with the ``X-Api-Version`` header. When absent a
deterministic default (the current stable version) is used. Unknown and
retired versions are rejected with error codes that are distinct from
authentication/authorization failures. Deprecated versions still work but
advertise ``Deprecation``/``Sunset`` and a successor.
"""
from __future__ import annotations

from datetime import datetime, timezone

from gateway.models import VersionInfo, VersionStatus

# Deterministic default: the latest *stable* (non-deprecated) version.
DEFAULT_VERSION = "v2"

_VERSIONS: list[VersionInfo] = [
    VersionInfo(
        version="v0",
        status=VersionStatus.RETIRED,
        retirement=datetime(2025, 1, 1, tzinfo=timezone.utc),
        successor="v2",
    ),
    VersionInfo(
        version="v1",
        status=VersionStatus.DEPRECATED,
        deprecated=True,
        sunset=datetime(2026, 12, 1, tzinfo=timezone.utc),
        retirement=datetime(2027, 6, 1, tzinfo=timezone.utc),
        successor="v2",
    ),
    VersionInfo(version="v2", status=VersionStatus.SUPPORTED),
    VersionInfo(version="v3", status=VersionStatus.SUPPORTED),
]

VERSIONS: dict[str, VersionInfo] = {v.version: v for v in _VERSIONS}
KNOWN_VERSIONS = frozenset(VERSIONS)


def resolve_version(requested: str | None) -> VersionInfo:
    """Resolve an explicit version, or the deterministic default.

    Raises ``KeyError`` with a ``VersionStatus`` marker so the caller can
    distinguish UNKNOWN from RETIRED without string matching.
    """
    name = (requested or DEFAULT_VERSION).strip().lower()
    info = VERSIONS.get(name)
    if info is None:
        raise KeyError(VersionStatus.UNKNOWN)
    if info.status is VersionStatus.RETIRED:
        raise KeyError(VersionStatus.RETIRED)
    return info


def deprecation_headers(info: VersionInfo) -> dict[str, str]:
    """RFC 9745-style deprecation headers for a deprecated version."""
    if info.status is not VersionStatus.DEPRECATED:
        return {}
    headers = {"Deprecation": "true"}
    if info.sunset is not None:
        headers["Sunset"] = info.sunset.strftime("%a, %d %b %Y %H:%M:%S GMT")
    if info.successor:
        headers["Link"] = f'</docs/versions>; rel="successor-version"; {info.successor}'
        headers["X-Successor-Version"] = info.successor
    if info.retirement is not None:
        headers["X-Retirement-Date"] = info.retirement.date().isoformat()
    return headers
