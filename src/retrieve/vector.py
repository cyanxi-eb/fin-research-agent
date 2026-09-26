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
from src.ingest import index_vector

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
    """向量索引：归一化矩阵 + 行对齐的元数据。

    元数据在加载时**摊平成 numpy 数组**（`_codes/_years/_sections`）：
    过滤是每次查询都要做的热路径，对 2650 条 Python dict 逐条比较不值得。
    """

    def __init__(self, matrix: np.ndarray, meta: list[dict], manifest: dict):
        self.matrix = matrix
        self.meta = meta
        self.manifest = manifest
        # 用 dtype=object 存字符串，缺失值统一成 ""（而不是 None）——
        # 这样比较时不用到处判 None，`"" == "600519"` 自然为 False
        self._codes = np.array([str(m.get("code") or "") for m in meta], dtype=object)
        self._sections = np.array([str(m.get("section") or "") for m in meta], dtype=object)
        self._years = np.array([int(m.get("year") or 0) for m in meta], dtype=np.int64)

    @classmethod
    def load(cls) -> "VectorIndex":
        mat, meta, manifest = index_vector.load()
        return cls(mat, meta, manifest)

    # ---------- 检索 ----------

    def search(self, query: str, *, topk: int | None = None,
               code: str | None = None, year: int | None = None,
               section: str | None = None) -> list[dict]:
        """语义召回 top-k。

        **过滤在打分之后、截断之前**，与 `BM25Index.search` 同规则 ——
        否则"限定公司后取 top5"会静默变成"全局 top5 里筛出 2 条"。
        """
        topk = topk or config.RETRIEVE_TOPK
        if self.matrix.shape[0] == 0:
            return []

        q = _cached_query_vector(query)
        if q.shape[0] != self.matrix.shape[1]:
            raise RuntimeError(
                f"查询向量维度 {q.shape[0]} 与索引维度 {self.matrix.shape[1]} 不符"
                f"（当前模型 {active_spec().get('model')}，"
                f"建库模型 {self.manifest.get('model')}）—— 向量库需重建")

        rows = np.arange(self.matrix.shape[0])
        if code:
            rows = rows[self._codes[rows] == str(code)]
        if year:
            rows = rows[self._years[rows] == int(year)]
        if section:
            # 子串匹配（与 BM25 一致）：调用方可能只给"财务报告"这种大节名
            rows = rows[np.array([str(section) in s for s in self._sections[rows]],
                                 dtype=bool)]
        if rows.size == 0:
            return []

        scores = self.matrix[rows] @ q
        keep = rows[scores > config.VECTOR_MIN_SCORE]
        if keep.size == 0:
            return []
        keep_scores = self.matrix[keep] @ q

        k = min(topk, keep.size)
        # argpartition 只要 top-k 不要求全序 → O(n)。2650 条虽然也不慢，
        # 但过滤到公司维度后集合会变，用同一套写法更稳。
        part = np.argpartition(-keep_scores, k - 1)[:k]
        order = part[np.argsort(-keep_scores[part], kind="stable")]

        hits: list[dict] = []
        for rank, i in enumerate(order, start=1):
            m = self.meta[int(keep[i])]
            hits.append({
                "chunk_id": m.get("chunk_id"),
                "score": round(float(keep_scores[i]), 6),
                "cosine": round(float(keep_scores[i]), 6),
                "rank": rank,
                "code": m.get("code"), "company": m.get("company"),
                "year": m.get("year"), "page_no": m.get("page_no"),
                "section": m.get("section"), "part": m.get("part"),
                "parts_total": m.get("parts_total"),
            })
        return hits

    def stats(self) -> dict:
        return {
            "vectors": int(self.matrix.shape[0]),
            "dim": int(self.matrix.shape[1]) if self.matrix.ndim == 2 else 0,
            "model": self.manifest.get("model"),
            "backend": self.manifest.get("backend"),
            "companies": len({m.get("code") for m in self.meta}),
            "company_years": len({(m.get("code"), m.get("year")) for m in self.meta}),
            "built_at": self.manifest.get("built_at"),
        }


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
    ok, why = index_vector.verify_against(spec)
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
