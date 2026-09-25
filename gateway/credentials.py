"""凭据模型、存储与认证。

凭据状态区分：
- missing：请求未携带密钥（401 credential_missing）
- invalid：密钥不存在（401 credential_invalid）
- expired：密钥存在但不在任何一个密钥版本的有效期内（401 credential_expired）
- revoked：凭据被撤销（401 credential_revoked）
- 权限不足独立为 403 permission_denied。

密钥轮换：同一凭据可注册多个 Secret，各自有独立有效期 [valid_from, valid_until)，
轮换重叠期内新旧密钥都可认证通过；旧密钥到期后仅新密钥可用，配额不重置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from gateway.errors import AuthenticationError, PermissionDeniedError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Secret:
    """一个密钥版本（轮换期间一个凭据可有多个）。"""

    secret: str
    valid_from: datetime
    valid_until: datetime | None  # None 表示长期有效

    def is_active(self, now: datetime) -> bool:
        if now < self.valid_from:
            return False
        if self.valid_until is not None and now >= self.valid_until:
            return False
        return True


@dataclass
class Credential:
    credential_id: str
    secrets: list[Secret] = field(default_factory=list)
    scopes: frozenset[str] = frozenset()
    revoked: bool = False


class CredentialStore:
    def __init__(self, credentials: list[Credential] | None = None) -> None:
        self._by_secret: dict[str, Credential] = {}
        for cred in credentials or []:
            for sec in cred.secrets:
                if sec.secret in self._by_secret:
                    raise ValueError(f"重复注册的密钥: {sec.secret[:6]}...")
                self._by_secret[sec.secret] = cred
        self._credentials = {c.credential_id: c for c in (credentials or [])}

    def list_credentials(self) -> list[Credential]:
        return list(self._credentials.values())

    def authenticate(
        self, key: str | None, now: datetime | None = None
    ) -> Credential:
        """校验明文密钥，返回凭据；失败抛 AuthenticationError。"""
        now = now or _utcnow()
        if not key:
            raise AuthenticationError(
                "缺少凭据，请通过 X-Api-Key 请求头提供",
                code="credential_missing",
                status_code=401,
            )
        cred = self._by_secret.get(key)
        if cred is None:
            raise AuthenticationError(
                "凭据无效",
                code="credential_invalid",
                status_code=401,
            )
        if cred.revoked:
            raise AuthenticationError(
                "凭据已被撤销",
                code="credential_revoked",
                status_code=401,
                details={"credential_id": cred.credential_id},
            )
        # 找到匹配的密钥版本，判断是否至少有一个版本在有效期内
        if not any(s.secret == key and s.is_active(now) for s in cred.secrets):
            matched = next(s for s in cred.secrets if s.secret == key)
            raise AuthenticationError(
                "凭据已过期",
                code="credential_expired",
                status_code=401,
                details={
                    "credential_id": cred.credential_id,
                    "valid_from": matched.valid_from.isoformat(),
                    "valid_until": (
                        matched.valid_until.isoformat()
                        if matched.valid_until
                        else None
                    ),
                },
            )
        return cred

    def authorize(self, credential: Credential, required_scope: str) -> None:
        """校验路由所需 scope；不足抛 PermissionDeniedError。"""
        if required_scope not in credential.scopes:
            raise PermissionDeniedError(
                f"凭据缺少权限 {required_scope!r}",
                code="permission_denied",
                status_code=403,
                details={
                    "credential_id": credential.credential_id,
                    "required_scope": required_scope,
                },
            )
