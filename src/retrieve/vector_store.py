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

        根据 `FA_VECTOR_BACKEND` 选择实现：
          numpy  → NumpyVectorStore（默认，零外部依赖，精确余弦）
          qdrant → QdrantVectorStore（需 qdrant-client + 运行中 Qdrant 服务）
        """
        backend = config.VECTOR_BACKEND if hasattr(config, "VECTOR_BACKEND") else "numpy"
        backends = getattr(config, "VECTOR_BACKENDS", frozenset({"numpy"}))
        if backend not in backends:
            raise RuntimeError(
                f"未知向量存储后端：{backend}（可选：{sorted(backends)}）")
        if backend == "numpy":
            return NumpyVectorStore.load()
        if backend == "qdrant":
            return QdrantVectorStore.load()
        raise RuntimeError(f"未注册的向量存储后端：{backend}")

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


class QdrantVectorStore(VectorStore):
    """Qdrant 向量数据库实现（behind `FA_VECTOR_BACKEND=qdrant`）。

    与 NumpyVectorStore 的关键行为差异（均在本地代码里补平，对调用方透明）：

    | 点 | Qdrant 原生 | 本地补平 |
    |---|---|---|
    | 过滤顺序 | payload filter 在 ANN search **之前**（批次 2 契约 ① 是打分后截断前） | code/year 精确匹配走 Qdrant 原生 filter；section 子串匹配拉回本地 Python `in` 过滤 |
    | 召回精度 | HNSW ANN 近似（不是精确余弦） | 过召回 topk*5 + 本地 min_score 精确阈值过滤 |
    | cosine | Qdrant cosine = dot product（向量已 L2 归一化，同 numpy） | —— |

    manifest.json + meta.jsonl **双后端共用** —— `index_vector.build()` 无论后端都会写这两个文件，
    `verify_against` 不需要改，仍然读同一个 `manifest.json`。
    """

    # 过召回因子：section 子串过滤可能砍掉一批候选，多拉回来再本地截断
    _OVERFETCH_FACTOR: int = 5

    def __init__(self, client, collection_name: str, manifest: dict, meta_by_id: dict):
        self._client = client
        self._collection = collection_name
        self._manifest = manifest
        # chunk_id → meta dict，用于 hits dict 拼装 + stats 聚合（Qdrant 不支持高效 distinct）
        self._meta_by_id = meta_by_id

    @classmethod
    def load(cls) -> "QdrantVectorStore":
        """加载 Qdrant collection + 本地 manifest/meta（延迟 import qdrant-client）。"""
        # 延迟 import —— 没装 qdrant-client 时 numpy 默认路径不受影响
        from qdrant_client import QdrantClient

        client = QdrantClient(url=config.QDRANT_URL)
        collection_name = config.QDRANT_COLLECTION

        # collection 存在性检查
        collections = [c.name for c in client.get_collections().collections]
        if collection_name not in collections:
            raise RuntimeError(
                f"Qdrant collection '{collection_name}' 不存在"
                f"（请先跑 `index_vector.build()` 写入 qdrant 分支）")

        # manifest 复用 NumpyVectorStore 的 manifest.json（双后端共用）
        manifest = index_vector.read_manifest()
        if not manifest:
            raise RuntimeError(
                f"向量库 manifest 缺失：{index_vector.MANIFEST_PATH}（请重建）")

        # meta_by_id 从 meta.jsonl 读（双后端共用）
        meta_rows = index_vector.load_meta()
        meta_by_id: dict[str, dict] = {}
        for m in meta_rows:
            cid = m.get("chunk_id")
            if cid is not None:
                meta_by_id[str(cid)] = m

        return cls(client, collection_name, manifest, meta_by_id)

    # ---------- VectorStore 接口 ----------

    def is_loaded(self) -> bool:
        try:
            info = self._client.get_collection(self._collection)
            return (info.points_count or 0) > 0
        except Exception:
            return False

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
        dim = int(self._manifest.get("dim", -1))
        if dim <= 0:
            return []

        # 维度检查（契约 ⑩，同 NumpyVectorStore 错误消息格式）
        if query_vec.shape[0] != dim:
            try:
                cur_model = active_spec().get("model")
            except Exception:
                cur_model = "unknown"
            raise RuntimeError(
                f"查询向量维度 {query_vec.shape[0]} 与索引维度 {dim} 不符"
                f"（当前模型 {cur_model}，"
                f"建库模型 {self._manifest.get('model')}）—— 向量库需重建")

        # ---- 构造 Qdrant payload filter（code/year 精确匹配）----
        qdrant_filter = self._build_qdrant_filter(code=code, year=year)

        # ---- 过召回：section 子串过滤会砍掉候选 ----
        fetch_count = max(topk * self._OVERFETCH_FACTOR, topk) if section else topk

        # qdrant-client 1.19+ 用 query_points 替代 search
        response = self._client.query_points(
            collection_name=self._collection,
            query=query_vec.tolist(),
            query_filter=qdrant_filter,
            limit=fetch_count,
            with_payload=True,
            score_threshold=(min_score if min_score > 0 else None),
        )
        search_result = response.points if response else []

        if not search_result:
            return []

        # ---- 本地后滤：section 子串 + min_score 精确阈值 ----
        filtered: list[tuple[float, object]] = []
        section_str = str(section) if section else None
        for point in search_result:
            payload = point.payload or {}

            # section 子串匹配（Qdrant 不原生支持）
            if section_str and section_str not in str(payload.get("section") or ""):
                continue

            # min_score 精确阈值（Qdrant 的 score_threshold 是 ANN 近似阈值，
            # 最终分数仍需本地精确比较以保证契约一致）
            score = float(point.score)
            if min_score > 0 and score <= min_score:
                continue

            filtered.append((score, point))

        if not filtered:
            return []

        # ---- 排序 + top-k ----
        filtered.sort(key=lambda x: -x[0])
        filtered = filtered[:topk]

        # ---- 拼 hits dict ----
        hits: list[dict] = []
        for rank, (score, point) in enumerate(filtered, start=1):
            payload = point.payload or {}
            # 优先从 meta_by_id 取（字段与 NumpyVectorStore.search 一致），
            # 没有就用 payload 兜底
            m = self._meta_by_id.get(str(payload.get("chunk_id") or ""), payload)
            hits.append({
                "chunk_id": m.get("chunk_id"),
                "score": round(score, 6),
                "cosine": round(score, 6),
                "rank": rank,
                "code": m.get("code"), "company": m.get("company"),
                "year": m.get("year"), "page_no": m.get("page_no"),
                "section": m.get("section"), "part": m.get("part"),
                "parts_total": m.get("parts_total"),
            })
        return hits

    @staticmethod
    def _build_qdrant_filter(*, code: str | None, year: int | None):
        """构造 Qdrant payload filter（code/year 精确匹配）。

        返回 None 表示无过滤条件；否则返回 `qdrant_client.models.Filter`。
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        must: list[FieldCondition] = []
        if code:
            must.append(FieldCondition(key="code", match=MatchValue(value=str(code))))
        if year:
            must.append(FieldCondition(key="year", match=MatchValue(value=int(year))))
        if not must:
            return None
        return Filter(must=must)

    def stats(self) -> dict:
        # vectors 从 Qdrant collection 状态取（高效）
        try:
            info = self._client.get_collection(self._collection)
            vectors = info.points_count or 0
        except Exception:
            vectors = 0

        # companies / company_years 从 meta_by_id 算（Qdrant 不支持高效 distinct 聚合）
        all_meta = self._meta_by_id.values()
        return {
            "vectors": vectors,
            "dim": int(self._manifest.get("dim", 0)),
            "model": self._manifest.get("model"),
            "backend": "qdrant",
            "companies": len({m.get("code") for m in all_meta}),
            "company_years": len({(m.get("code"), m.get("year")) for m in all_meta}),
            "built_at": self._manifest.get("built_at"),
        }

    def manifest(self) -> dict:
        return dict(self._manifest)
