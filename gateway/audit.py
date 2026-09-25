"""审计日志。

记录每一次准入判定（放行与拒绝均记录），可关联调用方与请求；
审计内容绝不包含明文凭据或完整密钥，只记录密钥指纹与凭据 ID。
日志同时输出到标准 logging（带 logger 名 gateway.audit），
并在内存中保留一份结构化记录，便于测试断言。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

AUDIT_LOGGER_NAME = "gateway.audit"


@dataclass
class AuditEntry:
    request_id: str
    event: str
    credential_id: str | None = None
    key_fingerprint: str | None = None
    route: str | None = None
    version: str | None = None
    decision: str = "allow"
    reason: str = ""
    idempotency_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AuditLogger:
    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        echo: bool = True,
        forbidden_values: set[str] | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(AUDIT_LOGGER_NAME)
        self._entries: list[AuditEntry] = []
        self.echo = echo
        # 运行时强制：这些明文值绝不能出现在任何审计记录中
        self._forbidden = {s for s in (forbidden_values or set()) if s}

    def register_secret(self, secret: str) -> None:
        if secret:
            self._forbidden.add(secret)

    def log(self, entry: AuditEntry) -> AuditEntry:
        # 防御：任何字段都不允许携带明文密钥（运行时断言，而不只是约定）
        blob = repr(entry)
        leaked = [s for s in self._forbidden if s in blob]
        if leaked:
            raise AssertionError(
                f"审计记录包含明文凭据片段（{len(leaked)} 项命中），已拒绝写出"
            )
        self._entries.append(entry)
        if self.echo:
            level = logging.WARNING if entry.decision == "deny" else logging.INFO
            self._logger.log(
                level,
                "audit event=%s decision=%s request_id=%s credential=%s "
                "key_fp=%s route=%s version=%s idempotency_key=%s reason=%s extra=%s",
                entry.event,
                entry.decision,
                entry.request_id,
                entry.credential_id,
                entry.key_fingerprint,
                entry.route,
                entry.version,
                entry.idempotency_key,
                entry.reason,
                entry.extra,
            )
        return entry

    def entries(self) -> list[AuditEntry]:
        """（测试用）返回内存中的审计记录。"""
        return list(self._entries)

    def reset(self) -> None:
        self._entries.clear()


def key_fingerprint(secret: str | None) -> str | None:
    """对密钥取指纹（sha256 前 12 位），禁止在审计中出现明文。"""
    if not secret:
        return None
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"
