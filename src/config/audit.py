from __future__ import annotations
import json
import os
from pathlib import Path

from .core import _api_key, _db_local, _secret
from .core import DB_DIR, DB_KEYS_LOCAL_PATH, LLM_KEYS_LOCAL_PATH
# ---- 人机协同（HITL，Step 5）----
#
# 只挂起**三种**情况，且都不含"确定性拒答"与"检索故障" —— 那两类是**结论/系统状态**，
# 不是"待确认"。把它们塞进待办队列，队列会变成垃圾场，真正需要人看的反而被埋掉。
#
#   1. citation_unsupported : 答案里的数值/引用找不到支撑（可能是检索错页，也可能模型转述有误）
#   2. low_confidence       : 证据薄但没到拒答线（能答，但不该自动发出去）
#   3. numeric_mismatch     : 工具层返回值与库中最新值不一致（并发写入/口径变更）
#
# 阈值 0.4 的依据：`answer._confidence` 是启发式证据充分度（0.7×单段覆盖率 + 0.3×引用数占比）。
# 实测正常问题 0.67~1.00、越界问题已被更早的闸门拦掉；0.4 落在"证据明显偏薄"的一侧，
# 且**要求未拒答**，因此不与拒答闸门重叠 —— 这是"分流"而不是"二次拒答"。
HITL_ENABLED: bool = os.getenv("HITL_ENABLED", "1").strip() == "1"
HITL_MIN_CONFIDENCE: float = float(os.getenv("HITL_MIN_CONFIDENCE", "0.4"))

# HITL **演练开关**（默认空 = 关闭）。
#
# 为什么需要它：真实触发 HITL 的条件里，`low_confidence` 要看模型输出、
# `numeric_mismatch` 要等库被并发改写 —— 都不能稳定复现，于是"挂起 → 跨进程恢复 →
# 人工确认 → 定稿"这条链路**没法在 CI/验收里跑**。加了这个开关，设置
# `FA_HITL_FORCE_REASON=low_confidence` 就能强制挂起一次。
#
# 它**只覆盖"要不要挂起"这个结论，不改动任何判定逻辑**：`verify` 仍然照常算出
# unsupported / dangling / drift 与真实置信度，只是最终把原因替换成指定值。
# 因此它不会掩盖真问题 —— 而"能复现"是验收的前置条件，比"纯粹"更重要。
HITL_FORCE_REASON: str = os.getenv("FA_HITL_FORCE_REASON", "").strip()

# ---- 用户体系与鉴权（本轮新增，修不足清单 G-11）----
#
# 端点守卫用 **FastAPI 依赖**（`Depends(current_user)`）而不是全局中间件：
# 中间件看不到"这条路径是不是需要登录"的路由信息，只能靠路径字符串硬匹配，
# 加一个端点就要同步改一份路径表，漏改就是静默裸奔。依赖挂在端点上则"忘了加"是显式的。
AUTH_ENABLED: bool = os.getenv("FA_AUTH_ENABLED", "1").strip() == "1"
# 默认 **1** 是**安全默认**：容器化部署时忘了配环境变量，宁可起不来也不要裸奔。
# 但 tests/conftest.py 把它钉成 0，让既有确定性用例跑免鉴权形态（见该文件注释）。
#
# 解析顺序：环境变量 FA_JWT_SECRET > data/db_keys.local.json 的 jwt_secret > 空串。
# **空串 + AUTH_ENABLED=1 = 启动即失败**（见 auth.require_jwt_secret）：
# 用一个写死在源码里的默认密钥签名，等于所有 token 任何拿到源码的人都能伪造，
# 而这种"看起来有鉴权其实没有"的状态从响应上完全看不出来 —— 必须让它响。
JWT_SECRET: str = _secret("FA_JWT_SECRET", "jwt_secret")
JWT_ALGORITHM: str = "HS256"
# access 短、refresh 长：access 是每次请求都要带的（过期太短会频繁刷新），
# refresh 只在换 token 时用（过期太长等于口令泄露后永久可用，7 天是折中）。
JWT_ACCESS_TTL_MIN: int = int(os.getenv("FA_JWT_ACCESS_TTL_MIN", "120"))
JWT_REFRESH_TTL_DAYS: int = int(os.getenv("FA_JWT_REFRESH_TTL_DAYS", "7"))

# 公开端点白名单（不需要 token）。**登录与健康检查必须在里面**：
# 否则"登录要 token、拿 token 要登录"，以及"健康检查要鉴权导致探活永远失败"。
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/", "/api/health",
    "/api/auth/login", "/api/auth/register", "/api/auth/refresh",
})
# 受保护的路径前缀（含 /api/auth/me 与 logout —— "我是谁"与"我登出"必须先证明身份）。
# 这份表同时供 /api/health 与前端展示"哪些端点要登录"，与实际依赖必须一致。
AUTH_PROTECTED_PREFIXES: frozenset[str] = frozenset({
    "/api/ask", "/api/hitl", "/api/citations", "/api/compare", "/api/audit",
    "/api/auth/me", "/api/auth/logout",
})

# 演示账号：口令为**空则不建号**并在启动日志里显式提示。
#
# 为什么不在源码里写死一个默认口令：那是最高危的一类默认凭据 ——
# 部署上线后没人会记得改，而任何看过仓库的人都能直接登进来。
# 无口令时"不建号 + 响亮提示"比"建一个 admin/admin123"负责得多。
SEED_ADMIN_USER: str = os.getenv("SEED_ADMIN_USER", "admin").strip() or "admin"
SEED_ADMIN_PASSWORD: str = _secret("SEED_ADMIN_PASSWORD", "seed_admin_password")
# 新账号口令长度下限。8 位是"能挡住随手试的弱口令"与"不强迫用户去用密码管理器"的折中。
AUTH_MIN_PASSWORD_LEN: int = int(os.getenv("FA_AUTH_MIN_PASSWORD_LEN", "8"))
