"""向量存储抽象接口（Phase 2 批次 2）。

从 `src/retrieve/vector.py`（读路径）+ `src/ingest/index_vector.py`（写路径）
里抽出 `VectorStore` 接口，让后端存储替换（批次 3 Qdrant / Milvus / pgvector）
变成"加一个实现 + 环境变量切"，而不是重写检索路径。

当前唯一实现：`NumpyVectorStore` —— 精确余弦 + 本地文件（vectors.npy + meta.jsonl + manifest.json）。
批次 3 会加 `QdrantVectorStore`（behind `FA_VECTOR_BACKEND=qdrant`）。

接口设计取舍：
- `search()` 直接返回 `list[dict]`（格式与 `VectorIndex.search()` 的 hits 一致）。
  VectorStore 内部持有 meta，filter/打分/拼 dict 一气呵成，不让调用方再拼。
- 工厂方法 `load()` 按默认位置加载。自定义位置用构造参数。
- `manifest()` 返回构建指纹（模型/维度/条数），`verify_against(spec)` 用它做同源检查。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from src import config
from src.embedding import EmbeddingUnavailable, active_spec
from src.ingest import index_vector


class VectorStore(ABC):
    """抽象向量存储。"""

    @classmethod
    def load(cls) -> "VectorStore":
        """按 `config.VECTOR_DIR` 默认位置加载。

        批次 3 会根据 `FA_VECTOR_BACKEND` 选择 Numpy / Qdrant 实现；
        批次 2 只注册 NumpyVectorStore，保持与当前行为完全一致。
        """
        backend = (config.VECTOR_BACKEND if hasattr(config, "VECTOR_BACKEND") else None) or "numpy"
        if backend == "numpy":
            return NumpyVectorStore.load()
        raise RuntimeError(f"未知向量存储后端：{backend}（批次 2 只支持 numpy）")

    @classmethod
    def verify_against(cls, spec: dict) -> tuple[bool, str]:
        """检查当前 embedding 通道与已落库的向量库是否同源（同模型同维度）。"""
        manifest = index_vector.read_manifest()
        if not manifest:
            return False, "向量库 manifest 缺失（请重建）"
        if manifest.get("model") != spec.get("model"):
            return False, (f"向量库由模型 {manifest.get('model')} 构建，"
                           f"当前通道是 {spec.get('model')} —— 必须重建")
        if int(manifest.get("dim", -1)) <= 0:
            return False, "向量库 manifest 里的维度非法（请重建）"
        return True, "ok"

    @abstractmethod
    def is_loaded(self) -> bool: ...

    @abstractmethod
    def search(
        self,
        query_vec: np.ndarray,
        *,
        topk: int,
        code: str | None = None,
        year: int | None = None,
        section: str | None = None,
        min_score: float = 0.0,
    ) -> list[dict]:
        """语义召回 top-k，返回 `VectorIndex.search()` 同格式 hits。

        每个 hit dict 含 chunk_id / score / cosine / rank / code / company /
        year / page_no / section / part / parts_total。
        """
        ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def manifest(self) -> dict: ...


class NumpyVectorStore(VectorStore):
    """默认实现：`vectors.npy` + `meta.jsonl` + `manifest.json`。

    精确余弦（矩阵已 L2 归一化 → 点积），不是 ANN。2650 × 1024 float32 ≈ 11 MB，
    单次 matmul < 1ms。批次 3 切 Qdrant 后保持 numpy 作为 fallback。
    """

    def __init__(self, matrix: np.ndarray, meta: list[dict], manifest: dict):
        self.matrix = matrix
        self.meta = meta
        self._manifest = manifest
        self._codes = np.array([str(m.get("code") or "") for m in meta], dtype=object)
        self._sections = np.array([str(m.get("section") or "") for m in meta], dtype=object)
        self._years = np.array([int(m.get("year") or 0) for m in meta], dtype=np.int64)

    @classmethod
    def load(cls) -> "NumpyVectorStore":
        mat, meta, manifest = index_vector.load()
        return cls(mat, meta, manifest)

    @classmethod
    def from_paths(cls, vectors_path: Path, meta_path: Path, manifest_path: Path) -> "NumpyVectorStore":
        mat = np.load(vectors_path)
        meta = index_vector.load_meta()
        manifest = index_vector.read_manifest() or {}
        if mat.shape[0] != len(meta):
            raise RuntimeError(f"向量行数 {mat.shape[0]} ≠ 元数据 {len(meta)} 行")
        return cls(mat, meta, manifest)

    # ---------- VectorStore 接口 ----------

    def is_loaded(self) -> bool:
        return self.matrix is not None and self.matrix.shape[0] > 0

    def search(
        self,
        query_vec: np.ndarray,
        *,
        topk: int,
        code: str | None = None,
        year: int | None = None,
        section: str | None = None,
        min_score: float = 0.0,
    ) -> list[dict]:
        topk = topk or config.RETRIEVE_TOPK
        if self.matrix.shape[0] == 0:
            return []
        if query_vec.shape[0] != self.matrix.shape[1]:
            try:
                cur_model = active_spec().get("model")
            except Exception:
                cur_model = "unknown"
            raise RuntimeError(
                f"查询向量维度 {query_vec.shape[0]} 与索引维度 {self.matrix.shape[1]} 不符"
                f"（当前模型 {cur_model}，"
                f"建库模型 {self._manifest.get('model')}）—— 向量库需重建")

        # 过滤（打分之后截断之前，与 BM25Index.search 同规则）
        rows = np.arange(self.matrix.shape[0])
        if code:
            rows = rows[self._codes[rows] == str(code)]
        if year:
            rows = rows[self._years[rows] == int(year)]
        if section:
            rows = rows[np.array([str(section) in s for s in self._sections[rows]], dtype=bool)]
        if rows.size == 0:
            return []

        scores = self.matrix[rows] @ query_vec
        keep = rows[scores > min_score]
        if keep.size == 0:
            return []
        keep_scores = self.matrix[keep] @ query_vec

        k = min(topk, keep.size)
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
            "model": self._manifest.get("model"),
            "backend": self._manifest.get("backend"),
            "companies": len({m.get("code") for m in self.meta}),
            "company_years": len({(m.get("code"), m.get("year")) for m in self.meta}),
            "built_at": self._manifest.get("built_at"),
        }

    def manifest(self) -> dict:
        return dict(self._manifest)
