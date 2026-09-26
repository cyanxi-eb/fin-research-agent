"""Phase 3 输入消毒工具 — Pydantic v2 field_validator 用的纯函数。

设计原则：fail-fast（发现非法内容直接抛 ValueError）而非静默替换。
- 空白压缩：strip + 连续空白 → 单空格
- 危险字符：禁止 NUL、控制字符（tab/newline 保留，便于多行输入）
- 最大长度：依赖 Pydantic Field(max_length=N)，这里不重复
- HTML/script 标签：strip 掉（防止 XSS 存进 db 再前端渲染）

注意：这些只防"用户不小心/恶意构造异常输入"，不是完整 WAF。
真正防 prompt 注入靠 LLM 侧的 system prompt + output 约束。
"""
from __future__ import annotations

import re

# ---- 危险字符模式 ----
# 禁止 NUL + 控制字符（保留 \\t \\n \\r，其他 \\x00-\\x1f \\x7f 全 strip）
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 连续空白压缩（strip 之后内部多个空白 → 单空格）
_MULTI_WS_RE = re.compile(r"[ \t]+")

# HTML/script 标签（简单 strip，防用户存 <script>alert(1)</script> 进 db）
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_text(value: str) -> str:
    """通用消毒 — strip + 去控制字符 + 去 HTML + 压缩空白。

    适用于：question、note、label、actor、reviewer 等自由文本字段。
    """
    if not isinstance(value, str):
        raise ValueError(f"expected str, got {type(value).__name__}")

    s = value.strip()
    s = _CONTROL_CHARS_RE.sub("", s)
    s = _HTML_TAG_RE.sub("", s)
    # 保留 newline（多行 note/question 合理），只压空格/tab
    s = "\n".join(_MULTI_WS_RE.sub(" ", line) for line in s.split("\n"))

    # strip 后空串 → 原始就是空白，Pydantic min_length 会拦，但 double check
    if not s.strip():
        raise ValueError("field cannot be blank")

    return s


def sanitize_identifier(value: str) -> str:
    """标识符类字段（username、thread_id、actor、mode、intent、decision）。

    严格模式：只允许字母数字 + 常见分隔符（_-@.），长度限制由 Field 管。
    """
    if not isinstance(value, str):
        raise ValueError(f"expected str, got {type(value).__name__}")

    s = value.strip()
    # 标识符允许的字符集：字母数字 + _-@.
    if not re.fullmatch(r"[A-Za-z0-9_\-@\.]+", s):
        raise ValueError(
            "only alphanumeric characters and '-_@.' are allowed"
        )
    return s


def sanitize_code(value: str) -> str:
    """股票代码 — 6 位数字，大写化。"""
    if not isinstance(value, str):
        raise ValueError(f"expected str, got {type(value).__name__}")
    s = value.strip().upper()
    if not re.fullmatch(r"\d{6}", s):
        raise ValueError("stock code must be exactly 6 digits")
    return s
