"""HTTP error types and JSON bodies for explainable rejections.

Every rejection carries a stable machine-readable ``error.code`` plus a human
message, so callers (and tests) can distinguish version negotiation failures
from authentication, authorization, quota and idempotency failures.
"""
from __future__ import annotations

from typing import Any

from gateway.models import RejectReason


class GatewayError(Exception):
    def __init__(self, status_code: int, code: RejectReason, message: str,
                 extra: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
        self.headers = headers or {}


def error_body(code: RejectReason, message: str,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code.value,
            "message": message,
        }
    }
    if extra:
        body["error"]["details"] = extra
    return body
