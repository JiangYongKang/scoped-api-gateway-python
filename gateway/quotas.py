"""按凭据、按路由的配额控制（asyncio 并发安全）。

固定时间窗口计数：
- 只有准入通过且真正执行业务的请求才计数（acquire 在业务执行前占用）；
- 业务失败（抛异常）时由准入层调用 release 回滚，失败/被拒绝的请求绝不计数；
- 幂等重放不计数（准入层对重放直接返回，不调用 acquire）；
- 超限抛 QuotaExceededError（429），details 含 retry_after_seconds 与窗口用量；
- 每个 (凭据, 路由) 一把异步锁，保证并发下 check-and-increment 原子、无重复计数。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from gateway.errors import QuotaExceededError


@dataclass(frozen=True)
class QuotaPolicy:
    limit: int
    window_seconds: int


@dataclass
class _Window:
    started_at: float
    count: int = 0


class QuotaManager:
    def __init__(
        self,
        policies: dict[str, QuotaPolicy] | None = None,
        *,
        default_policy: QuotaPolicy = QuotaPolicy(limit=100, window_seconds=60),
        clock=time.monotonic,
    ) -> None:
        self._policies = policies or {}
        self._default = default_policy
        self._clock = clock
        self._windows: dict[tuple[str, str], _Window] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def policy_for(self, route: str) -> QuotaPolicy:
        return self._policies.get(route, self._default)

    async def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _roll_window(self, win: _Window, policy: QuotaPolicy, now: float) -> _Window:
        if now - win.started_at >= policy.window_seconds:
            return _Window(started_at=now)
        return win

    async def acquire(self, credential_id: str, route: str) -> int:
        """检查并占用一个配额，返回当前窗口已用量（含本次）。"""
        policy = self.policy_for(route)
        key = (credential_id, route)
        lock = await self._lock_for(key)
        async with lock:
            now = self._clock()
            win = self._roll_window(self._windows.get(key, _Window(now)), policy, now)
            if win.count >= policy.limit:
                retry_after = max(
                    1, int(policy.window_seconds - (now - win.started_at))
                )
                self._windows[key] = win
                raise QuotaExceededError(
                    f"凭据 {credential_id} 对路由 {route} 的配额已用尽 "
                    f"({win.count}/{policy.limit})",
                    code="quota_exceeded",
                    status_code=429,
                    retryable=True,
                    details={
                        "credential_id": credential_id,
                        "route": route,
                        "limit": policy.limit,
                        "used": win.count,
                        "window_seconds": policy.window_seconds,
                        "retry_after_seconds": retry_after,
                    },
                )
            win.count += 1
            self._windows[key] = win
            return win.count

    async def release(self, credential_id: str, route: str) -> None:
        """业务失败时回滚一次占用；与 acquire 持同一把锁，杜绝丢失更新。"""
        key = (credential_id, route)
        lock = await self._lock_for(key)
        async with lock:
            win = self._windows.get(key)
            if win is not None:
                win.count = max(0, win.count - 1)

    def used(self, credential_id: str, route: str) -> int:
        win = self._windows.get((credential_id, route))
        return win.count if win else 0

    def reset(self) -> None:
        self._windows.clear()
        self._locks.clear()
