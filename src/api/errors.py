"""错误码字典（Phase 0-2 契约归一第一部分）。

纯数据字典，零运行时依赖 —— 任何模块都可以 `from src.api.errors import ERROR_CODES` 查表。
响应信封 `{code, msg, data, trace_id}` 延后到 Phase 1（前后端同步改），
所以目前错误码只用于 HTTPException.detail 的结构化信息、审计日志分类。
"""

from __future__ import annotations

from typing import Final

# --- 成功 ---
OK: Final[int] = 0

# --- 客户端错误 4xxx ---
BAD_REQUEST: Final[int] = 4001          # 参数格式错 / 缺字段
UNAUTHORIZED: Final[int] = 4010          # 未登录 / token 无效
TOKEN_EXPIRED: Final[int] = 4011         # token 过期
TOKEN_TYPE_ERROR: Final[int] = 4012      # access/refresh 混用
FORBIDDEN: Final[int] = 4030             # 已登录但角色不足
NOT_FOUND: Final[int] = 4040             # 资源不存在（thread_id / 公司 / 条目）
METHOD_NOT_ALLOWED: Final[int] = 4050
CONFLICT: Final[int] = 4090              # 用户名已占用 / HITL 状态冲突
INVALID_INPUT: Final[int] = 4220         # Pydantic 校验失败的人类可读等价

# --- 服务端错误 5xxx ---
INTERNAL_ERROR: Final[int] = 5000        # 未预期异常
INDEX_UNAVAILABLE: Final[int] = 5030     # 向量索引 / BM25 索引不存在
LLM_UNAVAILABLE: Final[int] = 5031       # 大模型通道未就绪
REGULATION_UNAVAILABLE: Final[int] = 5032
DB_UNAVAILABLE: Final[int] = 5033

# --- 业务领域错误（7xxx，不映射 HTTP status，响应里 ok=false）---
REFUSAL_NO_SOURCE: Final[int] = 7001           # 无出处可核验 → 拒答
REFUSAL_LOW_CONFIDENCE: Final[int] = 7002      # 置信度过低
REFUSAL_CROSS_VALIDATION: Final[int] = 7003    # 联网交叉验证失败
NUMERIC_MISMATCH: Final[int] = 7100            # 数值题多来源不一致
HITL_PENDING: Final[int] = 7200                # 挂起等人工

# --- 查表 ---
ERROR_CODES: Final[dict[int, str]] = {
    OK: "ok",
    BAD_REQUEST: "bad_request",
    UNAUTHORIZED: "unauthorized",
    TOKEN_EXPIRED: "token_expired",
    TOKEN_TYPE_ERROR: "token_type_error",
    FORBIDDEN: "forbidden",
    NOT_FOUND: "not_found",
    METHOD_NOT_ALLOWED: "method_not_allowed",
    CONFLICT: "conflict",
    INVALID_INPUT: "invalid_input",
    INTERNAL_ERROR: "internal_error",
    INDEX_UNAVAILABLE: "index_unavailable",
    LLM_UNAVAILABLE: "llm_unavailable",
    REGULATION_UNAVAILABLE: "regulation_unavailable",
    DB_UNAVAILABLE: "db_unavailable",
    REFUSAL_NO_SOURCE: "refusal_no_source",
    REFUSAL_LOW_CONFIDENCE: "refusal_low_confidence",
    REFUSAL_CROSS_VALIDATION: "refusal_cross_validation",
    NUMERIC_MISMATCH: "numeric_mismatch",
    HITL_PENDING: "hitl_pending",
}


def describe(code: int) -> str:
    """错误码 → 简短描述（给人读的）。"""
    return ERROR_CODES.get(code, f"unknown_code_{code}")
