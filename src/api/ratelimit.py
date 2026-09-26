"""Phase 2 速率限制 — 自建内存滑动窗口 middleware。

特性开关：FA_RATE_LIMIT_ENABLED=1 时启用。默认 OFF。

设计决策：不用 SlowAPI —— 它要求每个 endpoint 必须有 request: Request 参数，
26 处端点签名改动太侵入。这里用轻量自建实现：
- 滑动窗口（每个 IP + 前缀 独立计数，超过窗口时间自动过期）
- 路径前缀匹配（"auth" → /api/auth/*）
- 依赖 FastAPI Request.client.host（IP）+ scope["path"]（路径）

限流规则：
| 前缀 | 限制 | 说明 |
|---|---|---|
| /api/auth | 5/min | login/refresh/register 防暴力 |
| /api/ingest | 3/min | 入库高危操作 |
| /api/ask | 15/min | 问答主路径（宽松） |
| /api/compare | 10/min | 对比 |
| 其他 | 不限 | — |

存储：内存 dict（单机够用）。未来换 Redis 只需改 _Counter 实现。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Awaitable, Callable

from fastapi import Request
from starlette.responses import JSONResponse, Response


def is_rate_limit_enabled() -> bool:
    """特性开关读取。"""
    import os as _os
    return _os.environ.get("FA_RATE_LIMIT_ENABLED", "0") == "1"


# ============================================================
# 规则表：(路径前缀, 窗口秒数, 允许请求数)
# ============================================================
_RULES: list[tuple[str, int, int]] = [
    ("/api/auth",      60, 5),   # 5 per minute
    ("/api/ingest",    60, 3),   # 3 per minute
    ("/api/ask",       60, 15),  # 15 per minute
    ("/api/compare",   60, 10),  # 10 per minute
]


class _SlidingCounter:
    """单个 key 的滑动窗口计数器（线程安全 — asyncio 单线程，无需 lock）。"""

    def __init__(self, window_seconds: int) -> None:
        self.window = window_seconds
        self.timestamps: deque[float] = deque()

    def count(self) -> int:
        now = time.monotonic()
        # 过期清理
        cutoff = now - self.window
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        return len(self.timestamps)

    def inc(self) -> int:
        self.timestamps.append(time.monotonic())
        return self.count()


# key = (ip, path_prefix) → counter
# defaultdict 调 _SlidingCounter() 无参，但我们需要 window_seconds ——
# 所以在 _match_rule 调完拿到 window 后再创建/查找 counter。
_counters: dict[tuple[str, str], _SlidingCounter] = {}


def _ensure_counter(ip: str, path: str, window: int) -> _SlidingCounter:
    key = (ip, path)
    c = _counters.get(key)
    if c is None:
        c = _SlidingCounter(window)
        _counters[key] = c
    return c


def _match_rule(path: str) -> tuple[int, int] | None:
    """路径 → (window_seconds, max_requests)。最长前缀优先。"""
    best: tuple[str, int, int] | None = None
    for prefix, window, limit in _RULES:
        if path.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, window, limit)
    if best is None:
        return None
    return best[1], best[2]


async def rate_limit_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """请求计数 → 超限返回 429。"""
    if not is_rate_limit_enabled():
        return await call_next(request)

    path = request.scope.get("path", "")
    rule = _match_rule(path)
    if rule is None:
        return await call_next(request)

    window, limit = rule

    # IP 取法：优先 X-Forwarded-For 第一个，其次 remote_addr
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    elif request.client:
        ip = request.client.host or "unknown"
    else:
        ip = "unknown"

    counter = _ensure_counter(ip, path, window)
    current = counter.count()

    if current >= limit:
        retry_after = max(1, window // 3)
        return JSONResponse(
            status_code=429,
            content={"detail": f"rate limit exceeded ({limit}/{window}s)"},
            headers={"Retry-After": str(retry_after)},
        )

    counter.inc()
    return await call_next(request)


def reset_counters() -> None:
    """测试用：清空所有计数器。"""
    _counters.clear()
