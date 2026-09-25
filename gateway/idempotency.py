"""写操作幂等存储（asyncio 并发安全）。

规则：
- 同一幂等键的重复提交返回首次结果（不重复执行、不重复计配额、不重复产生副作用）；
- 同键不同载荷（或不同凭据/路由复用键）-> IdempotencyConflictError（409）；
- 同键并发：第二个请求等待首个完成后重放其结果；
- 首请求业务失败：清除进行中记录并唤醒等待者，等待者以同载荷成为新的首请求重试；
- 载荷指纹采用规范化 JSON（忽略键序），无法解析时退化为原始字节哈希。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from gateway.errors import IdempotencyConflictError

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"
MAX_KEY_LENGTH = 200


@dataclass
class IdempotencyRecord:
    key: str
    credential_id: str
    route: str
    payload_hash: str
    response: dict[str, Any] | None = None
    status_code: int = 200
    completed: bool = False


class IdempotencyStore:
    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._guard = asyncio.Lock()

    def _check_compatible(
        self,
        rec: IdempotencyRecord,
        credential_id: str,
        route: str,
        payload_hash: str,
    ) -> None:
        if rec.credential_id != credential_id or rec.route != route:
            raise IdempotencyConflictError(
                "幂等键已被其它凭据或路由使用",
                code="idempotency_key_reused",
                status_code=409,
                details={"idempotency_key": rec.key},
            )
        if rec.payload_hash != payload_hash:
            raise IdempotencyConflictError(
                "同一幂等键提交了不同的请求载荷",
                code="idempotency_payload_conflict",
                status_code=409,
                details={"idempotency_key": rec.key},
            )

    async def begin_or_wait(
        self,
        key: str,
        credential_id: str,
        route: str,
        payload_hash: str,
    ) -> tuple[IdempotencyRecord, bool]:
        """开始或加入一个幂等键。

        返回 (记录, is_first)：is_first=True 时调用方执行业务并 complete/fail；
        False 时记录中已有首次响应，调用方直接重放。
        """
        if not key:
            raise IdempotencyConflictError(
                "写操作必须提供非空的 Idempotency-Key",
                code="idempotency_key_missing",
                status=400,
            )
        if len(key) > MAX_KEY_LENGTH:
            raise IdempotencyConflictError(
                f"幂等键长度超过 {MAX_KEY_LENGTH}",
                code="idempotency_key_invalid",
                status_code=400,
            )
        while True:
            async with self._guard:
                rec = self._records.get(key)
                if rec is None:
                    rec = IdempotencyRecord(
                        key=key,
                        credential_id=credential_id,
                        route=route,
                        payload_hash=payload_hash,
                    )
                    self._records[key] = rec
                    self._events[key] = asyncio.Event()
                    return rec, True
                # 已有记录：无论进行中还是已完成，不兼容立即拒绝
                self._check_compatible(rec, credential_id, route, payload_hash)
                if rec.completed:
                    return rec, False
                event = self._events[key]
            # 等待首个请求完成（在锁外等待，不阻塞首请求）
            await event.wait()
            # 首请求失败会清除记录 -> 回到循环，本次成为新的首请求

    def complete(
        self, record: IdempotencyRecord, status_code: int, response: dict[str, Any]
    ) -> None:
        record.status_code = status_code
        record.response = response
        record.completed = True
        event = self._events.get(record.key)
        if event is not None:
            event.set()

    def fail(self, record: IdempotencyRecord) -> None:
        """首请求业务失败：清除记录并唤醒等待者（允许同键重试）。"""
        event = self._events.pop(record.key, None)
        self._records.pop(record.key, None)
        if event is not None:
            event.set()

    def reset(self) -> None:
        self._records.clear()
        self._events.clear()


def hash_payload(body: bytes, *, content_type: str | None = None) -> str:
    """计算载荷指纹：JSON 按规范化形式（键序无关），否则用原始字节。"""
    if not body:
        return "sha256:empty"
    try:
        obj = json.loads(body.decode("utf-8"))
        canonical = json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError):
        canonical = body
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
