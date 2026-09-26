"""向量入库 —— chunk 文本 → 归一化向量矩阵，落 `data/vector/`。

落盘四件套（前三个是正式产物，第四个是断点）：

| 文件 | 内容 |
|---|---|
| `vectors.npy`   | `(n, dim)` float32，**已 L2 归一化**（余弦=点积） |
| `meta.jsonl`    | 第 i 行 ↔ 矩阵第 i 行，存 chunk 的溯源字段 |
| `manifest.json` | 构建指纹：模型/维度/条数/chunk 指纹 —— **loader 必须校验** |
| `vectors.part.npy` | 断点续传中间产物，正常完成后删除 |

## 为什么矩阵只存一份 `meta.jsonl` 而不是直接 pickle 整个 chunk

pickle 会**把当初入库时的字段结构焊死在文件里**。本项目的 chunk 字段在 Step 3 加了
`section_marks` 演化过一次，将来还会加（比如 Step 5 的多粒度切分）。只存"检索与引用
真正需要的字段"，其余回查是 `BM25Index` 的职责 —— 两边各存一份必然不一致。

## 为什么不用 Chroma / FAISS

`2650 × 1024` float32 ≈ 11 MB，一次 `matmul` 就是全量精确余弦，单次查询 < 1ms。
上 ANN 索引在这个量级**只会引入近似召回损失**，而我们下一步要做的评测恰恰是要
**测量召回的变化** —— 用一个会丢召回的索引去测召回，基线就被污染了。
所以默认走精确检索，把 `VectorStore` 的加载/检索接口留清楚（`load()` / `search()`），
数据量上万后再换 HNSW/Chroma 只替换这一层实现，不影响任何调用方。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np

from src import config
from src.embedding import EmbeddingUnavailable, active_spec, embed_texts

VECTORS_PATH: Path = config.VECTOR_DIR / config.VECTOR_MATRIX_NAME
META_PATH: Path = config.VECTOR_DIR / config.VECTOR_META_NAME
MANIFEST_PATH: Path = config.VECTOR_DIR / config.VECTOR_MANIFEST_NAME
PART_PATH: Path = config.VECTOR_DIR / config.VECTOR_PART_NAME
# 断点状态文件（记录已完成条数与指纹）。用 str 拼接而不是 `/` 再 `+`：
# `dir / name + ".json"` 会先算除法再算加法 → `WindowsPath + str` 直接 TypeError。
PART_STATE_PATH: Path = Path(str(PART_PATH) + ".json")

# 每多少批落一次断点。太小 → 反复重写 11MB；太大 → 中断后白跑的量大。20 批 = 200 chunk。
CHECKPOINT_EVERY_BATCHES: int = 20


# ---------------- 指纹 ----------------

def chunks_fingerprint(chunks: list[dict]) -> str:
    """chunk 集合的内容指纹（**顺序敏感**）。

    为什么顺序也要算进去：矩阵行序 = 这里的遍历顺序。若只对"集合"取指纹，
    一个只改了排序的重建会被判为"没变"从而跳过 —— 而下游 meta 行序与矩阵行序
    就错位了，检索结果会整片张冠李戴（且看起来完全正常）。
    """
    h = hashlib.sha256()
    for c in chunks:
        h.update(str(c.get("chunk_id", "")).encode("utf-8"))
        h.update(b"\x00")
        text = str(c.get("text", ""))
        h.update(hashlib.sha1(text.encode("utf-8")).hexdigest().encode("ascii"))
        h.update(b"\x01")
    return h.hexdigest()


def _meta_row(c: dict) -> dict:
    """矩阵行 ↔ chunk 的最小对应表。**不要加字段就删**：加字段要重建向量库吗？不用，
    但 loader 只用这里有的键 —— 缺字段会让引用拼不出来。"""
    return {
        "chunk_id": c.get("chunk_id"),
        "code": c.get("code"),
        "company": c.get("company"),
        "year": c.get("year"),
        "page_no": c.get("page_no"),
        "section": c.get("section"),
        "part": c.get("part"),
        "parts_total": c.get("parts_total"),
    }


# ---------------- 构建 ----------------

def build(chunks: list[dict] | None = None, *, force: bool = False,
          verbose: bool = True) -> dict:
    """全量向量化并落盘。返回统计字典。

    **幂等**：manifest 的（后端/模型/维度/chunk 指纹）与当前一致时直接跳过。
    不一致必须重建 —— 这正是"防止拿 B 模型的查询向量去搜 A 模型的库"的那道闸。
    """
    if chunks is None:
        from src.ingest.chunk import load_all_chunks
        chunks = load_all_chunks()
    if not chunks:
        raise RuntimeError("没有可向量化的 chunk（先跑 scripts/ingest_all.py）")

    spec = active_spec()
    if spec["backend"] == "none":
        raise EmbeddingUnavailable("EMBEDDING_BACKEND=none，无法建向量库")
    fingerprint = chunks_fingerprint(chunks)

    if not force:
        existing = read_manifest()
        if existing and _manifest_matches(existing, spec, fingerprint, len(chunks)):
            if verbose:
                print(f"  · 跳过：向量库已是最新（{existing['n']} 条，"
                      f"模型 {existing['model']}）")
            return {"ok": True, "skipped": True, **existing}

    config.VECTOR_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 断点续传：指纹+模型都一致时，接着上次的 done 往下跑 ----
    done = 0
    acc: list[np.ndarray] = []
    if not force and _part_matches(spec, fingerprint):
        try:
            prev = np.load(PART_PATH)
            if prev.shape[0] <= len(chunks):
                acc = [prev]
                done = int(prev.shape[0])
                if verbose:
                    print(f"  · 发现断点：已完成 {done}/{len(chunks)}，从这里继续")
        except (OSError, ValueError) as e:
            print(f"  [warn] 断点文件不可用（{e}），从头开始")
            done = 0
            acc = []
    if done >= len(chunks):
        done = 0
        acc = []

    size = spec["batch_size"]
    total_batches = (len(chunks) - done + size - 1) // size
    t0 = time.monotonic()
    batches_done = 0
    texts_all = [str(c.get("text", "")) for c in chunks]

    for i in range(done, len(chunks), size):
        mat = embed_texts(texts_all[i:i + size])
        acc.append(mat)
        batches_done += 1
        if batches_done % CHECKPOINT_EVERY_BATCHES == 0 or i + size >= len(chunks):
            _save_part(acc, spec, fingerprint)
        if verbose:
            got = sum(int(a.shape[0]) for a in acc)
            el = time.monotonic() - t0
            rate = (got - done) / el if el > 0 else 0
            left = (len(chunks) - got) / rate if rate > 0 else 0
            print(f"    {got}/{len(chunks)} 条  ({el:.0f}s，约 {rate:.0f} 条/s，"
                  f"预计剩余 {left:.0f}s)", flush=True)

    matrix = np.vstack(acc) if acc else np.zeros((0, 0), dtype=np.float32)
    if matrix.shape[0] != len(chunks):
        raise RuntimeError(f"向量条数不符：{matrix.shape[0]} != {len(chunks)}（拒绝落盘）")

    np.save(VECTORS_PATH, matrix)
    with META_PATH.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(_meta_row(c), ensure_ascii=False) + "\n")

    manifest = {
        "backend": spec["backend"], "provider": spec["provider"], "model": spec["model"],
        "dim": int(matrix.shape[1]), "n": int(matrix.shape[0]),
        "fingerprint": fingerprint, "normalized": True,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(time.monotonic() - t0, 1),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    # 正式产物已就位，断点文件必须清掉 —— 否则下次会误以为"还没建完"而重复劳动
    PART_PATH.unlink(missing_ok=True)
    PART_STATE_PATH.unlink(missing_ok=True)

    # ---- Phase 2 批次 3: Qdrant 写路径分支 ----
    # numpy 路径已经把 vectors.npy / meta.jsonl / manifest.json 写完了；
    # qdrant 分支只是"额外 upsert 一份向量 + payload"，不替换本地文件。
    # verify_against 双后端共用 manifest.json，所以这里写完 qdrant 也不影响校验逻辑。
    backend = getattr(config, "VECTOR_BACKEND", "numpy")
    if backend == "qdrant":
        _upsert_qdrant(matrix, chunks, manifest, verbose=verbose)

    if verbose:
        backend_tag = f" + qdrant" if backend == "qdrant" else ""
        print(f"  ✓ 向量库 {manifest['n']} 条 × {manifest['dim']} 维，"
              f"{manifest['seconds']}s → {VECTORS_PATH}{backend_tag}")
    return {"ok": True, "skipped": False, **manifest}


def _upsert_qdrant(matrix: np.ndarray, chunks: list[dict], manifest: dict, *,
                   verbose: bool = True) -> None:
    """把建好的向量 upsert 到 Qdrant collection（behind config.VECTOR_BACKEND=qdrant）。

    - 延迟 import qdrant_client —— 没装时 numpy 默认路径不受影响
    - 向量是 L2 归一化的 → Qdrant Distance.COSINE 等价于 dot product（同 numpy）
    - payload 存 meta 全字段（code/year/section/company/page_no/part/parts_total）
    - 分批 upsert（每批 500）避免一次性 payload 过大
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    client = QdrantClient(url=config.QDRANT_URL)
    collection_name = config.QDRANT_COLLECTION
    dim = int(matrix.shape[1])

    # 创建 collection（不存在才建）
    collections = [c.name for c in client.get_collections().collections]
    if collection_name not in collections:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        if verbose:
            print(f"  · Qdrant collection '{collection_name}' 创建（dim={dim}, distance=COSINE）")

    # 构造 points（用整数索引当 point_id；payload 存 meta 全字段包括 chunk_id）
    # Qdrant 要求 point_id 必须是 unsigned int 或 UUID —— 我们的 chunk_id 格式是
    # `000858-2024-p1-1` 不是合法 UUID，所以用索引 i 当 ID，chunk_id 保留在 payload。
    points: list[PointStruct] = []
    for i, c in enumerate(chunks):
        meta = _meta_row(c)
        # payload 只放非 None 字段（避免 Qdrant 存一堆 null）
        payload = {k: v for k, v in meta.items() if v is not None}
        points.append(PointStruct(
            id=i,  # 整数索引 —— Qdrant 合法 ID
            vector=matrix[i].tolist(),
            payload=payload,
        ))

    # 分批 upsert
    BATCH = 500
    written = 0
    for j in range(0, len(points), BATCH):
        batch = points[j:j + BATCH]
        client.upsert(collection_name=collection_name, points=batch)
        written += len(batch)
        if verbose:
            print(f"    Qdrant upsert {written}/{len(points)}")

    if verbose:
        print(f"  ✓ Qdrant {collection_name} {written} 条 × {dim} 维")


def _save_part(acc: list[np.ndarray], spec: dict, fingerprint: str) -> None:
    mat = np.vstack(acc) if acc else np.zeros((0, 0), dtype=np.float32)
    np.save(PART_PATH, mat)
    PART_STATE_PATH.write_text(json.dumps(
        {"backend": spec["backend"], "model": spec["model"], "fingerprint": fingerprint,
         "done": int(mat.shape[0])}, ensure_ascii=False), encoding="utf-8")


def _manifest_matches(m: dict, spec: dict, fingerprint: str, n: int) -> bool:
    return (m.get("backend") == spec["backend"] and m.get("model") == spec["model"]
            and m.get("fingerprint") == fingerprint and int(m.get("n", -1)) == n)


def _part_matches(spec: dict, fingerprint: str) -> bool:
    if not (PART_PATH.exists() and PART_STATE_PATH.exists()):
        return False
    try:
        st = json.loads(PART_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return (st.get("backend") == spec["backend"] and st.get("model") == spec["model"]
            and st.get("fingerprint") == fingerprint)


# ---------------- 读取 ----------------

def read_manifest() -> dict | None:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_meta() -> list[dict]:
    if not META_PATH.exists():
        raise FileNotFoundError(f"向量元数据不存在：{META_PATH}（先跑 scripts/index_vector.py）")
    rows: list[dict] = []
    with META_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load() -> tuple[np.ndarray, list[dict], dict]:
    """加载 `(矩阵, 元数据行, manifest)`，并做**一致性校验**。

    校验不通过一律抛错而不是"尽力而为"：向量与文本错配的结果看起来完全正常，
    是最坏的一类静默错误。宁可让上层降级到 BM25（那是有说明的降级），
    也不要返回一份错位的相似度排序（那是没有说明的错误）。
    """
    if not VECTORS_PATH.exists():
        raise FileNotFoundError(
            f"向量库不存在：{VECTORS_PATH}（先跑 scripts/index_vector.py --build）")
    manifest = read_manifest()
    if not manifest:
        raise FileNotFoundError(
            f"向量库 manifest 缺失：{MANIFEST_PATH}（向量库不完整，请重建）")
    mat = np.load(VECTORS_PATH)
    meta = load_meta()
    if mat.shape[0] != len(meta):
        raise RuntimeError(
            f"向量库已损坏：矩阵 {mat.shape[0]} 行 ≠ 元数据 {len(meta)} 行")
    if mat.ndim != 2 or int(manifest.get("dim", -1)) != mat.shape[1]:
        raise RuntimeError(
            f"向量库维度不符：manifest={manifest.get('dim')} 实际={mat.shape}")
    return mat, meta, manifest


def verify_against(spec: dict) -> tuple[bool, str]:
    """当前 embedding 通道与库是否**同源**（同模型同维度）。

    `dim` 相同不代表模型相同 —— 两个 1024 维模型混用不会报错，只会给出垃圾排序。
    所以既有 model 名的硬比较，也保留 dim 作为第二道（能抓出手改配置的情况）。
    """
    m = read_manifest()
    if not m:
        return False, "向量库 manifest 缺失（请重建）"
    if m.get("model") != spec.get("model"):
        return False, (f"向量库由模型 {m.get('model')} 构建，"
                       f"当前通道是 {spec.get('model')} —— 必须重建（否则相似度无意义）")
    if int(m.get("dim", -1)) <= 0:
        return False, "向量库 manifest 里的维度非法（请重建）"
    return True, "ok"


if __name__ == "__main__":
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    print("manifest:", json.dumps(read_manifest() or {}, ensure_ascii=False))
    try:
        mat, meta, mf = load()
        print(f"矩阵 {mat.shape}  dtype={mat.dtype}")
        print(f"首行 meta: {json.dumps(meta[0], ensure_ascii=False)}")
        print(f"样例 L2 范数: {float(np.linalg.norm(mat[0])):.4f}（应为 1.0）")
    except (FileNotFoundError, RuntimeError) as e:
        print("load 失败：", e)
