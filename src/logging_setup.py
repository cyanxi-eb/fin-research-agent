"""Phase 2 结构化日志 — Python 标准库 logging + JSON formatter + trace_id 注入。

特性开关：FA_STRUCTURED_LOGGING=1 时启用 JSON 格式，默认 OFF（人类可读 plain）。

设计：
- contextvar 存当前请求的 trace_id（middleware 写入，logging filter 读取）
- setup_logging() 在 server.py lifespan 启动时调用
- 零外部依赖（不用 structlog）
- 审计 audit.log() 继续写 db，和 logging 解耦 —— logging 是应用运行日志，
  audit 是业务操作记录，职责不同
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# ============ ContextVar ============
# 每个请求独立的 trace_id —— middleware 写入，filter 读取
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


def set_trace_id(trace_id: str) -> None:
    """middleware 调用：把当前请求的 trace_id 注入 logging context。"""
    _trace_id_var.set(trace_id)


def get_trace_id() -> str:
    """logging filter 调用：读取当前 trace_id。"""
    try:
        return _trace_id_var.get()
    except LookupError:
        return "-"


# ============ JSON Formatter ============
class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化成 JSON。零依赖。"""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": get_trace_id(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc_info"] = self.formatException(record.exc_info)
        # extra dict 里的字段合并进来（如果有）
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            entry.update(extra)
        return json.dumps(entry, ensure_ascii=False, default=str)


# ============ Setup ============
def setup_logging() -> None:
    """配置根 logger。幂等 — 多次调用不会重复 handler。"""
    if logging.getLogger().handlers:
        return

    structured = os.environ.get("FA_STRUCTURED_LOGGING", "0") == "1"
    level = os.environ.get("LOG_LEVEL", "INFO").upper()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    handler = logging.StreamHandler()
    if structured:
        handler.setFormatter(JsonFormatter())
    else:
        # plain 格式：时间 level [trace_id] logger: message
        plain_fmt = logging.Formatter(
            "%(asctime)s %(levelname)-8s %(trace_id)s %(name)s: %(message)s"
        )
        # 注入 trace_id 到 LogRecord — 用 Filter
        class _TraceIdInjector(logging.Filter):
            def filter(self, rec: logging.LogRecord) -> bool:
                if not hasattr(rec, "trace_id"):
                    rec.trace_id = get_trace_id()
                return True
        handler.addFilter(_TraceIdInjector())
        handler.setFormatter(plain_fmt)

    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """业务代码拿 logger 的便捷函数。"""
    return logging.getLogger(name)
