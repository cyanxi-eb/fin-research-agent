"""向量化客户端 —— 把「文本 → 向量」这件事收成**一条可降级的通道**。

为什么单独一层而不是在 index/vector 里各调一次 API：

1. **入库与查询必须用同一个模型、同一套预处理**。分开写迟早漂移；
   而"用 A 模型建的库、拿 B 模型的查询向量去搜"是最难发现的一类错
   —— 维度不同会立刻报错，维度相同就会**静默返回毫无意义的排序**。
   所以两者共用本模块，并把模型名/维度写进 manifest 供 loader 校验。
2. **降级要显式**：没有 Key、没装本地依赖、网络不通时，上层必须能
   "退回纯 BM25 并把这件事说出来"，而不是抛异常让整条链路挂掉。

两条通道（`config.EMBEDDING_BACKEND`）：

| backend | 依赖 | 说明 |
|---|---|---|
| `api`   | 只要 Key + 网络 | 任意 OpenAI 兼容 `/embeddings`，复用 `LLM_PROVIDERS` 的凭据 |
| `local` | sentence-transformers + torch | 离线可用，但要下模型权重（国内需 HF 镜像） |
| `none`  | — | 显式禁用，用于验证"纯 BM25 也能跑完整链路" |

实测要点（2026-09-22，见 `scripts/probe_embedding_sources.py`）：
- DashScope 兼容模式 `text-embedding-v4`：维度 **1024**，单次 0.6~1.1s，**batch 上限 10**
  （batch=25 返回 `HTTP 400 InvalidParameter: batch size is ...`）。
  「上限 10」这个数是**实测出来的**：不少文档写 v3/v4 支持 25，照抄就会在批量入库时炸。
- DeepSeek **没有** embeddings 接口（`/embeddings` → 404），不能想当然复用它的 Key。

返回向量**已做 L2 归一化**，所以下游的余弦相似度就是一次点积。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Sequence

import numpy as np

from src import config


class EmbeddingUnavailable(RuntimeError):
    """向量化通道不可用（没 Key / 没依赖 / 网络异常）。

    刻意用**独立异常类型**：上层的降级判断要能区分"向量化不可用（→ 退回 BM25）"
    与"代码写错了（→ 该炸就炸）"。用一个宽泛的 Exception 接住会把真 bug 也吞掉。
    """


# 上一次请求的时间戳（模块级节流，单进程脚本场景够用）
_LAST_CALL: float = 0.0
# 本地模型缓存（首次加载很慢，别每次重建）
_LOCAL_MODEL = None


def _throttle() -> None:
    global _LAST_CALL
    gap = time.monotonic() - _LAST_CALL
    if gap < config.EMBEDDING_INTERVAL:
        time.sleep(config.EMBEDDING_INTERVAL - gap)
    _LAST_CALL = time.monotonic()


# ---------------- 通道描述与可用性 ----------------

def active_spec() -> dict:
    """当前通道的完整描述（含凭据，供内部调用；对外用 `describe()`）。"""
    backend = (config.EMBEDDING_BACKEND or "").lower()
    if backend == "api":
        provider = config.EMBEDDING_PROVIDER
        profile = config.LLM_PROVIDERS.get(provider)
        if profile is None:
            raise EmbeddingUnavailable(
                f"未知的 EMBEDDING_PROVIDER「{provider}」，"
                f"可选：{sorted(config.LLM_PROVIDERS)}")
        return {"backend": "api", "provider": provider, "model": config.EMBEDDING_API_MODEL,
                "base_url": profile["base_url"], "api_key": profile["api_key"],
                "batch_size": config.EMBEDDING_BATCH_SIZE}
    if backend == "local":
        return {"backend": "local", "provider": "local", "model": config.EMBEDDING_MODEL,
                "base_url": None, "api_key": None, "batch_size": 32}
    if backend == "none":
        return {"backend": "none", "provider": None, "model": None,
                "base_url": None, "api_key": None, "batch_size": 1}
    raise EmbeddingUnavailable(
        f"未知的 EMBEDDING_BACKEND「{backend}」，可选：{sorted(config.EMBEDDING_BACKENDS)}")


def describe() -> dict:
    """脱敏描述（给 `/api/health` 用）—— 只报"配没配"，不回显 Key。

    与 `src/llm.is_ready()` 同样口径：`configured` 用真值判断，
    而不是"有兜底占位串"，否则未配置会被误报成已配置。
    """
    try:
        spec = active_spec()
    except EmbeddingUnavailable as e:
        return {"backend": (config.EMBEDDING_BACKEND or "").lower(), "ready": False,
                "reason": str(e)}
    ready, reason = embedding_ready()
    return {"backend": spec["backend"], "provider": spec["provider"],
            "model": spec["model"], "batch_size": spec["batch_size"],
            "key_configured": bool(str(spec["api_key"] or "").strip()),
            "ready": ready, "reason": reason}


def embedding_ready() -> tuple[bool, str]:
    """向量化能不能真的用（返回 `(就绪, 原因)`，原因是给人看的话）。"""
    try:
        spec = active_spec()
    except EmbeddingUnavailable as e:
        return False, str(e)
    if spec["backend"] == "none":
        return False, "EMBEDDING_BACKEND=none（已显式禁用向量通道）"
    if spec["backend"] == "api":
        if not str(spec["api_key"] or "").strip():
            return False, (f"向量通道 {spec['provider']} 未配置 API Key"
                           f"（可设环境变量或在 data/llm_keys.local.json 填写）")
        return True, "ok"
    # local：只判断依赖在不在，**不在这里加载模型**（加载要几秒，健康检查不该那么慢）
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False, ("本地向量通道需要 sentence-transformers"
                       "（pip install sentence-transformers；国内还需配 HF 镜像）")
    return True, "ok"


# ---------------- API 通道 ----------------

def _post_embeddings(spec: dict, texts: list[str]) -> list[list[float]]:
    """发一次批量请求，返回**按输入顺序**对齐的向量列表。"""
    url = spec["base_url"].rstrip("/") + "/embeddings"
    payload = json.dumps({"model": spec["model"], "input": texts,
                          "encoding_format": "float"}).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(1, config.EMBEDDING_RETRY + 1):
        _throttle()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Authorization": f"Bearer {spec['api_key']}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=config.EMBEDDING_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # 读 body 失败不该盖掉原始错误
                pass
            last_err = EmbeddingUnavailable(f"HTTP {e.code} {detail}")
            # 4xx 是参数问题（Key 失效 / 模型名错 / batch 超限），重试无意义 —— 立刻抛，
            # 把服务端原话带上（"batch size is ..." 这种提示直接指向要改的配置）
            if 400 <= e.code < 500:
                raise last_err
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = EmbeddingUnavailable(f"{type(e).__name__}: {e}")
        else:
            rows = body.get("data") or []
            if len(rows) != len(texts):
                raise EmbeddingUnavailable(
                    f"返回条数不符：请求 {len(texts)} 条、返回 {len(rows)} 条"
                    f"（模型 {spec['model']}）")
            # **按 `index` 字段重排，不假设返回顺序=输入顺序**。
            # 兼容端点里顺序错位的实现是存在的，一旦错位，向量与文本会整片错配，
            # 而错配后的检索结果"看起来完全正常"（都是真实片段，只是张冠李戴）。
            rows = sorted(rows, key=lambda r: r.get("index", 0))
            return [r.get("embedding") or [] for r in rows]

        if attempt < config.EMBEDDING_RETRY:
            time.sleep(min(2 ** attempt, 8))
    raise last_err or EmbeddingUnavailable("向量化请求失败")


# ---------------- 本地通道 ----------------

def _local_model():
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise EmbeddingUnavailable(
                "本地向量通道需要 sentence-transformers：pip install sentence-transformers") from e
        _LOCAL_MODEL = SentenceTransformer(config.EMBEDDING_MODEL)
    return _LOCAL_MODEL


def _local_embed(texts: list[str]) -> list[list[float]]:
    model = _local_model()
    vecs = model.encode(texts, normalize_embeddings=True, batch_size=32,
                        show_progress_bar=False)
    return [list(map(float, v)) for v in vecs]


# ---------------- 对外入口 ----------------

def _normalize(mat: np.ndarray) -> np.ndarray:
    """L2 归一化（零向量保持零向量，避免除零产生 NaN）。"""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


def embed_texts(texts: Sequence[str], *, batch_size: int | None = None,
                on_batch: Callable[[int, int], None] | None = None) -> np.ndarray:
    """把一批文本编码成归一化向量矩阵 `(n, dim)` float32。

    `on_batch(done, total)` 每批调用一次 —— 入库脚本靠它打进度与**落断点**：
    265 次请求的批量作业中断后不该从第一页重来。

    任何通道故障都抛 `EmbeddingUnavailable`（调用方据此降级），不返回空矩阵
    —— 返回空矩阵会让上层把"向量化失败"当成"检索不到"。
    """
    items = [str(t or "")[: config.EMBEDDING_MAX_CHARS] for t in texts]
    if not items:
        return np.zeros((0, 0), dtype=np.float32)

    spec = active_spec()
    if spec["backend"] == "none":
        raise EmbeddingUnavailable("EMBEDDING_BACKEND=none（已显式禁用向量通道）")

    size = batch_size or spec["batch_size"]
    chunks: list[list[float]] = []
    total = (len(items) + size - 1) // size
    for i in range(0, len(items), size):
        batch = items[i:i + size]
        if spec["backend"] == "api":
            chunks.extend(_post_embeddings(spec, batch))
        else:
            chunks.extend(_local_embed(batch))
        if on_batch:
            on_batch(len(chunks), len(items))
    mat = np.asarray(chunks, dtype=np.float32)
    if mat.ndim != 2 or mat.shape[0] != len(items):
        raise EmbeddingUnavailable(
            f"向量矩阵形状异常：{mat.shape}，期望 ({len(items)}, dim)")
    return _normalize(mat)


def embed_query(text: str) -> np.ndarray:
    """单条查询向量，形状 `(dim,)`。"""
    return embed_texts([text])[0]


if __name__ == "__main__":
    # 自检：python -m src.embedding ["查询文本"]
    import sys as _sys

    _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    print("describe:", json.dumps(describe(), ensure_ascii=False))
    q = _sys.argv[1] if len(_sys.argv) > 1 else "贵州茅台2024年的毛利率是多少"
    ok, why = embedding_ready()
    print(f"ready: {ok} ({why})")
    if ok:
        t0 = time.monotonic()
        v = embed_query(q)
        print(f"query vector: dim={v.shape[0]} norm={float(np.linalg.norm(v)):.4f} "
              f"({time.monotonic() - t0:.2f}s)")
        m = embed_texts(["贵州茅台的毛利率", "宁德时代的电池产能"])
        print(f"batch=2 -> {m.shape}, 两两余弦={float(m[0] @ m[1]):.4f}")
