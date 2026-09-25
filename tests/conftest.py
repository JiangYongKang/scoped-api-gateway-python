"""pytest 公共夹具：每个测试使用全新的应用实例（状态彼此隔离）。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app


@pytest.fixture()
def client() -> TestClient:
    logging.getLogger("gateway.audit").setLevel(logging.INFO)
    application = create_app()
    with TestClient(application) as c:
        yield c
