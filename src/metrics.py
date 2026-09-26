"""Phase 3 Prometheus metrics — 零业务侵入，middleware 收集。

启动：注册 middleware（每次请求自动采集）+ /metrics endpoint。

指标设计：
- http_requests_total: 计数器，method + path + status_code
- http_request_duration_seconds: Histogram，分桶 p50/p90/p99
- llm_calls_total: 计数器，provider + model + status
- llm_duration_seconds: Histogram，LLM 调用耗时
- ingest_rows_total: 计数器，入库格数
- exceptions_total: 计数器，异常类型

LLM/ingest 指标需要调用点手动 record（用 get_registry().labels(...).inc() 模式），
HTTP 指标由 middleware 自动完成。
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
    REGISTRY,
)
from starlette.responses import Response as StarletteResponse


# ============ HTTP 指标 ============
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


# ============ LLM 指标（调用点手动 record）============
LLM_CALLS_TOTAL = Counter(
    "llm_calls_total",
    "Total LLM calls",
    ["provider", "model", "status"],
)

LLM_DURATION = Histogram(
    "llm_duration_seconds",
    "LLM call latency",
    ["provider", "model"],
)


# ============ 业务指标 ============
INGEST_ROWS_TOTAL = Counter(
    "ingest_rows_total",
    "Total rows ingested",
)

EXCEPTIONS_TOTAL = Counter(
    "exceptions_total",
    "Total unhandled exceptions",
    ["type"],
)


async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """HTTP 请求自动采集。"""
    start = time.perf_counter()
    path = request.url.path

    response = await call_next(request)

    elapsed = time.perf_counter() - start
    # 路径归一（strip query params）
    path_normalized = path.split("?")[0]
    # 忽略 /metrics 自身的采集循环
    if path_normalized != "/api/metrics":
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method,
            path=path_normalized,
            status_code=str(response.status_code),
        ).inc()
        HTTP_REQUEST_DURATION.labels(
            method=request.method,
            path=path_normalized,
        ).observe(elapsed)

    return response


def metrics_endpoint() -> StarletteResponse:
    """GET /api/metrics → Prometheus text exposition format。"""
    return StarletteResponse(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )


def record_llm(provider: str, model: str, status: str, elapsed_s: float) -> None:
    """LLM 调用点调用：record_llm("dashscope", "qwen-max", "ok", 1.23)"""
    LLM_CALLS_TOTAL.labels(provider=provider, model=model, status=status).inc()
    LLM_DURATION.labels(provider=provider, model=model).observe(elapsed_s)


def record_ingest(rows: int) -> None:
    """入库点调用：record_ingest(4)"""
    INGEST_ROWS_TOTAL.inc(rows)


def record_exception(exc_type: str) -> None:
    """异常点调用：record_exception("ValueError")"""
    EXCEPTIONS_TOTAL.labels(type=exc_type).inc()
