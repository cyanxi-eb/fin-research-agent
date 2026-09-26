from __future__ import annotations
import json
import os
from pathlib import Path

from .core import _api_key
# ---- LLM 多供应商通道（均为 OpenAI 兼容协议，按需增删）----
# api_key 走环境变量或 data/llm_keys.local.json，双方皆无则为空串（界面会高亮提示）
# balance=True 表示支持余额查询（目前仅 DeepSeek 提供 /user/balance 接口）
LLM_PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek 深度求索",
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        "api_key": _api_key("DEEPSEEK_API_KEY", "deepseek"),
        # 官方 /models 实测（2026-09-21）：旧型号 deepseek-chat/deepseek-reasoner 已下线
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "supports_balance": True,
        # 金融场景要「宁可拒答不可瞎答」，比通用场景更低温度
        "temperature": 0.0,
    },
    "qwen": {
        "label": "通义千问 (DashScope)",
        "base_url": os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        "api_key": _api_key("QWEN_API_KEY", "qwen"),
        "models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-long"],
        "supports_balance": False,  # DashScope 兼容模式无余额查询接口
        "temperature": 0.0,
    },
    # 本地私有化通道（vLLM / Ollama），演示断网部署时切换到此供应商
    "local": {
        "label": "本地模型 (Ollama/vLLM)",
        "base_url": os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1"),
        "api_key": "ollama",
        "models": ["qwen2.5:7b-instruct"],
        "supports_balance": False,
        "temperature": 0.0,
    },
}
# 默认激活供应商（可被 data/llm_state.json 覆盖）；默认模型取该供应商 models[0]
LLM_ACTIVE: str = os.getenv("LLM_ACTIVE", "deepseek")

# ---- Embedding（Step 4）----
#
# 两条通道，**默认走 API**（本地通道是可选依赖，见末尾说明）：
#   api   = 任意 OpenAI 兼容 `/embeddings` 端点，复用 LLM_PROVIDERS 里的 base_url/api_key
#   local = sentence-transformers 本地模型（需 `pip install sentence-transformers`，拖进 torch）
#   none  = 显式禁用（用来验证"降级到纯 BM25 也要能跑完整链路"）
#
# 为什么默认 API 而不是本地模型：**成本差一个数量级**。
# 实测（scripts/probe_embedding_sources.py，2026-09-22）：
#   - DashScope 兼容模式 text-embedding-v4 → 维度 1024，单次调用 0.6~1.1s，**batch 上限 10**
#     （batch=25 直接 `HTTP 400 InvalidParameter: batch size is ...`——这个上限只能实测，
#      文档里 v3/v4 常被写成 25，照着写就会在批量入库时炸）
#   - 同一 Key 的 gte-rerank-v2 可用（top score 0.9447），所以升级/重排同一家供应商即可
#   - 本地通道当时未装 sentence-transformers；HF 直连国内不通，得配镜像
# 注意：**DeepSeek 没有 embeddings 接口**（`/embeddings` 返回 404），别想当然地复用它的 Key。
EMBEDDING_BACKEND: str = os.getenv("EMBEDDING_BACKEND", "api").strip().lower()
EMBEDDING_BACKENDS: frozenset[str] = frozenset({"api", "local", "none"})
# api 通道用哪个供应商凭据（取 LLM_PROVIDERS 里的 base_url / api_key）
EMBEDDING_PROVIDER: str = os.getenv("EMBEDDING_PROVIDER", "qwen")
EMBEDDING_API_MODEL: str = os.getenv("EMBEDDING_API_MODEL", "text-embedding-v4")
# ⚠️ 实测值，不是文档值：DashScope 这批模型**batch 上限 10**，超了直接 400 不降级
EMBEDDING_BATCH_SIZE: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
EMBEDDING_TIMEOUT: int = int(os.getenv("EMBEDDING_TIMEOUT", "30"))
EMBEDDING_RETRY: int = int(os.getenv("EMBEDDING_RETRY", "3"))
# 两次请求之间的最小间隔：2650 个 chunk ÷ batch 10 = 265 次请求，
# 不留间隔容易撞 QPS 限流（撞了会重试，反而更慢）。
EMBEDDING_INTERVAL: float = float(os.getenv("EMBEDDING_INTERVAL", "0.05"))
# 单条文本截断长度（模型上限 8192 token，chunk 本身 ~512 字，这里只是兜底）
EMBEDDING_MAX_CHARS: int = int(os.getenv("EMBEDDING_MAX_CHARS", "3000"))
# 本地通道模型名（仅 EMBEDDING_BACKEND=local 时使用）
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

# ---- 重排（Step 4，可降级）----
#   api         = DashScope 原生 text-rerank（实测 gte-rerank-v2 可用）
#   local       = sentence-transformers CrossEncoder
#   passthrough = 不做重排，原序返回（默认；开关打开才花钱）
#
# 默认关闭：重排是**每问一次就多一次 API 调用**，而它带来的收益要先用评测量化
# （eval/report.md 的 Step3/Step4 对比）才值得默认打开。宁可默认省，也不要默认贵。
RERANK_BACKEND: str = os.getenv("RERANK_BACKEND", "passthrough").strip().lower()
RERANK_BACKENDS: frozenset[str] = frozenset({"api", "local", "passthrough"})
RERANK_API_MODEL: str = os.getenv("RERANK_API_MODEL", "gte-rerank-v2")
# 用哪家凭据（DashScope 原生 rerank 不在 OpenAI 兼容路径下，所以单独配供应商）
RERANK_PROVIDER: str = os.getenv("RERANK_PROVIDER", "qwen")
# 单条文档送进重排前的截断长度（重排模型对单文档长度有限制，超了会整批 400）
RERANK_MAX_DOC_CHARS: int = int(os.getenv("RERANK_MAX_DOC_CHARS", "2000"))
RERANK_TOP_N: int = int(os.getenv("RERANK_TOP_N", "20"))   # 送给重排的候选数
RERANK_TIMEOUT: int = int(os.getenv("RERANK_TIMEOUT", "30"))
# 本地通道模型名（仅 RERANK_BACKEND=local 时使用）
RERANKER_MODEL: str = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
# DashScope 原生 rerank 的端点（不在 OpenAI 兼容路径下，要单独拼）
DASHSCOPE_RERANK_URL: str = os.getenv(
    "DASHSCOPE_RERANK_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank")
