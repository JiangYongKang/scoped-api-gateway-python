"""Shared fixtures: deterministic clock, seeded services, verbose logging."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.deps import FixedClock, build_services

SEED_TIME = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        force=True,
    )


_configure_logging()
log = logging.getLogger("tests")


@pytest.fixture
def clock():
    return FixedClock(SEED_TIME)


@pytest.fixture
def services(clock):
    return build_services(clock, quota_window_seconds=3600)


@pytest.fixture
def client(services):
    return TestClient(create_app(services))


def log_decision(case: str, resp, **inputs) -> None:
    """Print the key inputs and the gateway's decision basis for each case."""
    body = resp.json() if resp.headers.get("content-type", "").startswith(
        "application/json") else resp.text
    code = None
    if isinstance(body, dict) and "error" in body:
        code = body["error"]["code"]
    log.info("CASE=%s inputs=%s -> status=%s error_code=%s rate=%s/%s replay=%s",
             case, inputs, resp.status_code, code,
             resp.headers.get("x-ratelimit-used", "-"),
             resp.headers.get("x-ratelimit-limit", "-"),
             resp.headers.get("idempotent-replay", "-"))
