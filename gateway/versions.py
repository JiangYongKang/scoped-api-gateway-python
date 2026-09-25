"""API 版本注册表与协商。

版本生命周期：
- supported：当前受支持；
- deprecated：仍可用，但响应带 Deprecation/Sunset 提示；
- retired：已下线，请求被明确拒绝（HTTP 410，错误码 version_retired，
  与权限失败 401/403 明确可区分）。

未指定版本时使用固定默认版本（默认行为确定，不隐式升级到最新版）。
未知版本返回 HTTP 400 与错误码 version_unknown。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from gateway.errors import VersionError

DEFAULT_VERSION = "2024-01-01"
API_VERSION_HEADER = "X-Api-Version"
DEPRECATION_HEADER = "Deprecation"
SUNSET_HEADER = "Sunset"
LINK_HEADER = "Link"


class VersionStatus(str, Enum):
    SUPPORTED = "supported"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


@dataclass(frozen=True)
class VersionInfo:
    version: str
    status: VersionStatus
    sunset: str | None = None
    successor: str | None = None


class VersionRegistry:
    def __init__(
        self,
        versions: list[VersionInfo] | None = None,
        default_version: str = DEFAULT_VERSION,
    ) -> None:
        self._versions = {v.version: v for v in (versions or [])}
        self._default = default_version
        if self._default not in self._versions:
            raise ValueError(f"default version {self._default!r} 未注册")

    @property
    def default_version(self) -> str:
        return self._default

    def supported_versions(self) -> list[str]:
        return [
            v
            for v, info in self._versions.items()
            if info.status is not VersionStatus.RETIRED
        ]

    def resolve(self, requested: str | None) -> VersionInfo:
        """按显式请求或默认值解析版本，非法/下线版本在此被拒绝。"""
        if requested is None or requested == "":
            return self._versions[self._default]
        info = self._versions.get(requested)
        if info is None:
            raise VersionError(
                f"未知的 API 版本: {requested!r}",
                code="version_unknown",
                status_code=400,
                details={
                    "requested_version": requested,
                    "default_version": self._default,
                    "supported_versions": self.supported_versions(),
                },
            )
        if info.status is VersionStatus.RETIRED:
            raise VersionError(
                f"API 版本 {requested!r} 已下线",
                code="version_retired",
                status_code=410,
                details={
                    "requested_version": requested,
                    "sunset": info.sunset,
                    "successor": info.successor,
                    "supported_versions": self.supported_versions(),
                },
            )
        return info

    def negotiate(self, requested: str | None) -> VersionInfo:
        """协商版本；未指定时落到确定性的默认版本。"""
        return self.resolve(requested)


def default_registry() -> VersionRegistry:
    """内置版本表：

    - 2023-01-01: 已下线（410 version_retired）
    - 2024-01-01: 受支持，且是未指定版本时的默认版本
    - 2025-01-01: 已弃用但仍可用，带 Deprecation/Sunset 提示
    - 2026-01-01: 受支持的最新版本
    """
    return VersionRegistry(
        [
            VersionInfo(
                version="2023-01-01",
                status=VersionStatus.RETIRED,
                sunset="Sat, 01 Jul 2023 00:00:00 GMT",
                successor="2024-01-01",
            ),
            VersionInfo(version="2024-01-01", status=VersionStatus.SUPPORTED),
            VersionInfo(
                version="2025-01-01",
                status=VersionStatus.DEPRECATED,
                sunset="Thu, 01 Oct 2026 00:00:00 GMT",
                successor="2026-01-01",
            ),
            VersionInfo(version="2026-01-01", status=VersionStatus.SUPPORTED),
        ],
        default_version=DEFAULT_VERSION,
    )
