"""向量召回 —— 单路（语义）检索，与 `bm25.py` 并列的一路。

分三层职责，别混：
- `index_vector.py`（ingest 层）：怎么**建**、怎么**存**、怎么**校验**；
- 本模块：怎么**查**（查询向量缓存、元数据过滤、top-k）；
- `pipeline.py`：把本路与 BM25 路**融合**成最终 hits。

本模块只返回"候选 + 相似度"，**不返回正文**。正文一律回查 chunk 存储
（`BM25Index.chunks`，那是全项目唯一的正文来源）。为什么不做成两处各存一份正文：
正文是本项目**演进最快**的字段（Step 3 刚给 chunk 加了节名判定），存两份迟早不一致，
而不一致的正文会让"引用指向 P87，但引用片段显示的是另一页的文字"这种错误出现 —— 那比召不回来严重得多。

检索是**精确余弦**（矩阵已归一化 → 点积），不是 ANN。理由见 `index_vector.py` 模块头。
"""
from __future__ import annotations

from collections import OrderedDict

import numpy as np

from src import config
from src.embedding import EmbeddingUnavailable, active_spec, embed_query, embedding_ready
from src.retrieve.vector_store import VectorStore

# 查询向量缓存（LRU）。**评测时这一点很关键**：Step3/Step4 两轮跑同一批问题，
# 缓存保证两轮用的是同一份查询向量，差异只来自检索策略而不是 embedding 抖动。
_QUERY_CACHE: "OrderedDict[str, np.ndarray]" = OrderedDict()

_INDEX: "VectorIndex | None" = None


def _cached_query_vector(query: str) -> np.ndarray:
    hit = _QUERY_CACHE.get(query)
    if hit is not None:
        _QUERY_CACHE.move_to_end(query)
        return hit
    vec = embed_query(query)
    _QUERY_CACHE[query] = vec
    while len(_QUERY_CACHE) > config.VECTOR_QUERY_CACHE:
        _QUERY_CACHE.popitem(last=False)
    return vec


def clear_query_cache() -> None:
    _QUERY_CACHE.clear()


class VectorIndex:
    """向量索引门面 —— 持有 `VectorStore` 实例，对外提供 search / stats。

    查询向量 LRU 缓存在这里（评测关键：两轮跑同问题要用同一份查询向量，
    差异只来自检索策略而不是 embedding 抖动）。实际的矩阵操作、过滤、
    argpartition 全在 VectorStore 实现里 —— 批次 3 切 Qdrant 不影响本类。
    """

    def __init__(self, store_or_matrix, *args):
        """兼容两种构造方式：

        - 新方式：VectorIndex(VectorStore)
        - 旧方式（测试用）：VectorIndex(matrix, meta, manifest)
        """
        if args:
            from src.retrieve.vector_store import NumpyVectorStore
            self._store = NumpyVectorStore(store_or_matrix, args[0], args[1])
        else:
            self._store = store_or_matrix

    @classmethod
    def load(cls) -> "VectorIndex":
        return cls(VectorStore.load())

    # ---------- 检索 ----------

    def search(self, query: str, *, topk: int | None = None,
               code: str | None = None, year: int | None = None,
               section: str | None = None) -> list[dict]:
        """语义召回 top-k。

        **过滤在打分之后、截断之前**，与 `BM25Index.search` 同规则 ——
        否则"限定公司后取 top5"会静默变成"全局 top5 里筛出 2 条"。
        """
        q = _cached_query_vector(query)
        return self._store.search(
            q, topk=topk or config.RETRIEVE_TOPK,
            code=code, year=year, section=section,
            min_score=config.VECTOR_MIN_SCORE,
        )

    def stats(self) -> dict:
        return self._store.stats()

    def manifest(self) -> dict:
        return self._store.manifest()


def get_index() -> VectorIndex:
    """惰性单例（11MB 矩阵 + 2650 行元数据，别每次查重载）。"""
    global _INDEX
    if _INDEX is None:
        _INDEX = VectorIndex.load()
    return _INDEX


def reset_index() -> None:
    """丢缓存（测试 / 重建向量库后调用）。"""
    global _INDEX
    _INDEX = None


def status() -> dict:
    """向量通道可用性 —— 供 pipeline 决定是否降级、以及把**降级原因**说清楚。

    三层检查缺一不可：通道就绪（有 Key/依赖）→ 库存在且同源 → 能加载。
    任何一层不过，调用方拿到的是"可读的原因字符串"，可以直接写进答案的 notes。
    """
    ready, reason = embedding_ready()
    if not ready:
        return {"available": False, "reason": reason, "stage": "channel"}
    try:
        spec = active_spec()
    except EmbeddingUnavailable as e:
        return {"available": False, "reason": str(e), "stage": "channel"}
    ok, why = VectorStore.verify_against(spec)
    if not ok:
        return {"available": False, "reason": why, "stage": "manifest"}
    try:
        return {"available": True, "reason": "ok", "stage": "ok", **get_index().stats()}
    except (FileNotFoundError, RuntimeError, EmbeddingUnavailable) as e:
        return {"available": False, "reason": str(e), "stage": "load"}


if __name__ == "__main__":
    # 自检：python -m src.retrieve.vector "毛利率" [--code 600519] [--topk 5]
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    args = sys.argv[1:]
    query = args[0] if args and not args[0].startswith("--") else "毛利率"
    code = args[args.index("--code") + 1] if "--code" in args else None
    topk = int(args[args.index("--topk") + 1]) if "--topk" in args else 5

    print("status:", json.dumps(status(), ensure_ascii=False))
    st = status()
    if st["available"]:
        idx = get_index()
        print(f"索引: {idx.stats()}")
        print(f"\n查询「{query}」" + (f"（code={code}）" if code else "") + f" top{topk}：")
        for h in idx.search(query, topk=topk, code=code):
            print(f"  {h['rank']}. cos={h['cosine']:.4f}  {h['company']}{h['year']} "
                  f"P{h['page_no']} {(h['section'] or '')[:20]}  {h['chunk_id']}")
