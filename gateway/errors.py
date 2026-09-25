"""统一错误码与异常类型。

错误码设计目标：版本问题、认证问题、权限问题、配额问题、幂等冲突
必须彼此可区分，调用方依据 code 即可程序化处理。
"""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    """所有网关准入错误的基类。"""

    status_code: int = 400
    code: str = "gateway_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details: dict[str, Any] = details or {}
        self.retryable = retryable


class VersionError(GatewayError):
    """版本协商相关错误（与权限失败明确区分）。"""


class AuthenticationError(GatewayError):
    """凭据缺失/无效/过期/撤销。"""


class PermissionDeniedError(GatewayError):
    """凭据有效但权限不足。"""


class QuotaExceededError(GatewayError):
    """超过按凭据按路由的配额。"""


class IdempotencyConflictError(GatewayError):
    """同幂等键对应不同载荷。"""
