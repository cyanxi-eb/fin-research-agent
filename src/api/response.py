"""Phase 0-2 响应归一 + API 版本化 — 中间件 + 特性开关。

## 响应信封
特性开关：FA_RESPONSE_ENVELOPE=1 时启用响应信封 {code, msg, data, trace_id}。
默认 OFF — 484 测试零改动、前端零崩。

## API 版本化
URL rewrite middleware：/api/v1/* → /api/*
双路径同时可用（前端切 /api/v1，旧客户端继续走 /api 不崩）。
后端路由模块保持 /api/xxx 完整路径不变（零改动）。

## 中间件顺序（洋葱模型，server.py 注册顺序即执行顺序）
1. _request_id_middleware — 分配/透传 X-Request-Id
2. _api_version_rewrite_middleware — /api/v1/* → /api/*（前置 rewrite）
3. _response_envelope_middleware — 成功响应包壳
4. HTTPException handler — 错误响应包壳
"""
from __future__ import annotations

import json
import os
from typing import Awaitable, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .errors import (
    BAD_REQUEST,
    CONFLICT,
    FORBIDDEN,
    INTERNAL_ERROR,
    INVALID_INPUT,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    OK,
    UNAUTHORIZED,
)

# --- HTTP status → ERROR_CODE 映射 ---
HTTP_STATUS_TO_ERROR_CODE: dict[int, int] = {
    200: OK,
    201: OK,
    400: BAD_REQUEST,
    401: UNAUTHORIZED,
    403: FORBIDDEN,
    404: NOT_FOUND,
    405: METHOD_NOT_ALLOWED,
    409: CONFLICT,
    422: INVALID_INPUT,
    500: INTERNAL_ERROR,
}


ENVELOPE_HEADER = "X-Envelope"   # 标记：此响应已包过壳，middleware 跳过


def is_envelope_enabled() -> bool:
    """特性开关读取。"""
    return os.environ.get("FA_RESPONSE_ENVELOPE", "0") == "1"


def _make_envelope(code: int, msg: str, data, trace_id: str) -> dict:
    """构造统一信封。"""
    return {
        "code": code,
        "msg": msg,
        "data": data,
        "trace_id": trace_id,
    }


async def response_envelope_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """成功路径响应包壳。

    Starlette/FastAPI 内部会把所有响应包装成 _StreamingResponse，
    它只有 body_iterator 没有 .body —— 必须迭代 body_iterator 取完整 body，
    然后用 JSONResponse 重建。
    """
    if not is_envelope_enabled():
        return await call_next(request)

    response = await call_next(request)

    # skip 1: 非 API 路径
    if not request.url.path.startswith("/api/"):
        return response

    # skip 2: 已被 HTTPException handler 包过
    if response.headers.get(ENVELOPE_HEADER) == "1":
        return response

    # skip 3: 204 空响应
    if response.status_code == 204:
        return response

    # skip 4: 非 JSON content-type
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        return response

    # --- 从 body_iterator 取完整 body ---
    # Starlette 0.40+ 所有响应都是 _StreamingResponse，只有 body_iterator
    try:
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
    except AttributeError:
        # 某些响应类型没有 body_iterator（极少见），直接返回
        return response

    if not body:
        return response

    try:
        original = json.loads(body)
    except json.JSONDecodeError:
        return response

    trace_id = getattr(request.state, "request_id", "unknown")
    code = HTTP_STATUS_TO_ERROR_CODE.get(response.status_code, INTERNAL_ERROR)

    if code == OK:
        msg = "ok"
        data = original
    else:
        msg = original.get("detail", str(original))
        if isinstance(msg, list):
            msg = str(msg)
        data = original.get("data", original)

    envelope = _make_envelope(code=code, msg=msg, data=data, trace_id=trace_id)

    new_headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    new_headers[ENVELOPE_HEADER] = "1"

    return JSONResponse(
        content=envelope,
        status_code=response.status_code,
        headers=new_headers,
    )


async def http_exception_handler_envelope(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """HTTPException → 信封 JSONResponse。

    默认 OFF 时 behavior 与 FastAPI 原生 HTTPException handler 一致：
    {"detail": "..."} + 原始 status + exc.headers（OAuth2 客户端依赖 WWW-Authenticate）。
    """
    if not is_envelope_enabled():
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,  # ← 关键：401 必须带 WWW-Authenticate
        )

    trace_id = getattr(request.state, "request_id", "unknown")
    code = HTTP_STATUS_TO_ERROR_CODE.get(exc.status_code, INTERNAL_ERROR)
    msg = str(exc.detail) if exc.detail else ""
    if isinstance(msg, list):
        msg = str(msg)

    return JSONResponse(
        status_code=exc.status_code,
        content=_make_envelope(code=code, msg=msg, data=None, trace_id=trace_id),
        headers={ENVELOPE_HEADER: "1", **(exc.headers or {})},
    )


# ==================== API version rewrite ====================

V1_PREFIX = "/api/v1"   # 前端新路径
LEGACY_PREFIX = "/api"  # 后端内部路由路径（所有 APIRouter endpoint path 都从这里开始）


async def api_version_rewrite_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """/api/v1/* → /api/* 路径重写。

    FastAPI 用 request.url.path / request.scope["path"] 做路由匹配。
    我们改 scope["path"] 就行 — 这是 Starlette ASGI 层的"真相"，
    request.url.path 是它的只读视图（会自动同步）。

    同时改 raw_path（WSGI 某些网关层会读这个），保持两者一致。
    """
    path = request.scope.get("path", "")
    if path.startswith(V1_PREFIX + "/") or path == V1_PREFIX:
        new_path = LEGACY_PREFIX + path[len(V1_PREFIX):]
        request.scope["path"] = new_path
        raw_path = request.scope.get("raw_path")
        if isinstance(raw_path, bytes):
            request.scope["raw_path"] = new_path.encode("utf-8")

    response = await call_next(request)

    # 响应头加 V1-Accepted: 1 — 前端可以用来确认自己走的是 v1 路径
    # 但旧客户端走 /api 时不要加这个头（零侵入）
    return response
