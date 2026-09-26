"""向量层用例（纯内存、**不联网**：查询向量用替身，矩阵用手工构造的数据）。

向量检索最容易坏的地方不是"排序算错"，而是**静默错配** ——
矩阵第 i 行与元数据第 i 行对不上、查询向量与库不是同一个模型、候选找不到正文。
错配之后检索结果依然"看起来完全正常"（都是真实片段），所以用例重点全在这几条不变量上。
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from src import config
from src.embedding import EmbeddingUnavailable, active_spec, describe, embed_texts, embedding_ready
from src.ingest import index_vector
from src.retrieve import vector as vector_mod


def _meta(i: int, code: str = "600519", year: int = 2024, page: int = 10,
          section: str = "管理层讨论与分析") -> dict:
    return {"chunk_id": f"{code}-{year}-p{page}-{i}", "code": code, "company": "测试公司",
            "year": year, "page_no": page, "section": section, "part": i, "parts_total": 1}


def _index(rows: list[tuple[list[float], dict]]) -> vector_mod.VectorIndex:
    # 空输入时 np.asarray([]) 是 1 维，后面按 axis=1 求范数会炸 → 显式给 (0,2)
    mat = (np.asarray([r[0] for r in rows], dtype=np.float32) if rows
           else np.zeros((0, 2), dtype=np.float32))
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    return vector_mod.VectorIndex(mat, [r[1] for r in rows],
                                  {"model": "m", "dim": mat.shape[1], "backend": "api", "n": len(rows)})


@pytest.fixture
def patched_query(monkeypatch):
    """把查询向量换成可控值（否则每次 search 都要调一次 embedding API）。"""
    def _set(vec: list[float]):
        monkeypatch.setattr(vector_mod, "embed_query", lambda q: np.asarray(vec, dtype=np.float32))
        vector_mod.clear_query_cache()
    return _set


def test_search_ranks_by_cosine(patched_query):
    idx = _index([
        ([1.0, 0.0], _meta(1, page=1)),
        ([0.8, 0.6], _meta(2, page=2)),
        ([0.2, 0.98], _meta(3, page=3)),
    ])
    patched_query([1.0, 0.0])
    hits = idx.search("毛利率", topk=3)
    assert [h["page_no"] for h in hits] == [1, 2, 3]
    assert hits[0]["cosine"] == pytest.approx(1.0)
    assert [h["rank"] for h in hits] == [1, 2, 3]


def test_filters_apply_before_truncation(patched_query):
    """**先过滤再取 topk**，与 BM25 同规则。

    若反过来（先全局 topk 再筛），限定公司后只剩 1 条 —— 静默变少，
    而这正是"限定公司后还是答不出来"这类投诉的根源。
    """
    idx = _index([
        ([1.0, 0.0], _meta(1, code="600519", page=1)),
        ([0.99, 0.01], _meta(2, code="000858", page=2)),
        ([0.9, 0.1], _meta(3, code="000858", page=3)),
        ([0.5, 0.5], _meta(4, code="000858", page=4)),
    ])
    patched_query([1.0, 0.0])
    hits = idx.search("毛利率", topk=2, code="000858")
    assert [h["code"] for h in hits] == ["000858", "000858"]
    assert [h["page_no"] for h in hits] == [2, 3]      # 不是"全局 top2 里筛出的 1 条"


def test_year_and_section_filters(patched_query):
    idx = _index([
        ([1.0, 0.0], _meta(1, year=2024, section="管理层讨论与分析", page=1)),
        ([0.9, 0.1], _meta(2, year=2025, section="财务报告", page=2)),
    ])
    patched_query([1.0, 0.0])
    assert len(idx.search("q", topk=5, year=2024)) == 1
    assert len(idx.search("q", topk=5, section="财务")) == 1
    assert len(idx.search("q", topk=5, year=1999)) == 0


def test_nonpositive_cosine_is_dropped(patched_query):
    """负/零相关直接丢：它们不是"弱证据"，是"反证据"。"""
    idx = _index([([1.0, 0.0], _meta(1, page=1)), ([-1.0, 0.0], _meta(2, page=2))])
    patched_query([1.0, 0.0])
    assert [h["page_no"] for h in idx.search("q", topk=5)] == [1]


def test_dimension_mismatch_raises(patched_query):
    """维度不符必须报错（并提示重建），不能"尽力算一个"。

    维度相同但模型不同的情况抓不到 —— 那是 `verify_against` 的职责（比模型名）。
    """
    idx = _index([([1.0, 0.0, 0.0], _meta(1))])
    patched_query([1.0, 0.0])
    with pytest.raises(RuntimeError, match="维度"):
        idx.search("q", topk=1)


def test_empty_index_returns_empty(patched_query):
    idx = _index([])
    patched_query([1.0, 0.0])
    assert idx.search("q", topk=3) == []


def test_query_cache_returns_same_vector(monkeypatch):
    """同一问题必须复用同一份查询向量：评测要跑 Step3/Step4 两轮，
    两次拿到不同的向量会让差异里混进 embedding 抖动，指标就没法归因了。"""
    calls = {"n": 0}

    def fake(q):
        calls["n"] += 1
        return np.asarray([1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(vector_mod, "embed_query", fake)
    vector_mod.clear_query_cache()
    _index([([1.0, 0.0], _meta(1))]).search("同一个问题", topk=1)
    _index([([1.0, 0.0], _meta(1))]).search("同一个问题", topk=1)
    assert calls["n"] == 1


# ---------------- 入库侧的不变量 ----------------

def test_fingerprint_is_order_sensitive():
    """指纹**对顺序敏感**：矩阵行序 = chunk 遍历序，只改排序也算变了。

    若指纹与顺序无关，一个"只改了 chunk 顺序"的重建会被判为"没变"而跳过，
    于是矩阵与 meta 行序错位 —— 检索结果开始张冠李戴，而且看不出来。
    """
    a = [{"chunk_id": "1", "text": "甲"}, {"chunk_id": "2", "text": "乙"}]
    b = [{"chunk_id": "2", "text": "乙"}, {"chunk_id": "1", "text": "甲"}]
    assert index_vector.chunks_fingerprint(a) != index_vector.chunks_fingerprint(b)
    assert index_vector.chunks_fingerprint(a) == index_vector.chunks_fingerprint(list(a))


def test_fingerprint_detects_content_change():
    a = [{"chunk_id": "1", "text": "甲"}]
    b = [{"chunk_id": "1", "text": "甲。"}]
    assert index_vector.chunks_fingerprint(a) != index_vector.chunks_fingerprint(b)


def test_manifest_matches_requires_model_and_fingerprint():
    spec = {"backend": "api", "model": "text-embedding-v4"}
    fp = "abc"
    ok = {"backend": "api", "model": "text-embedding-v4", "fingerprint": "abc", "n": 3}
    assert index_vector._manifest_matches(ok, spec, fp, 3)          # noqa: SLF001
    # 换个模型（哪怕维度一样）必须判为不匹配 → 重建
    bad_model = {**ok, "model": "text-embedding-v3"}
    assert not index_vector._manifest_matches(bad_model, spec, fp, 3)   # noqa: SLF001
    # chunk 变了同样必须重建
    assert not index_vector._manifest_matches(ok, spec, "def", 3)       # noqa: SLF001


def test_meta_row_keeps_traceability_fields():
    """meta 行必须带齐溯源字段：向量路只存这些，缺一个就拼不出引用。"""
    row = index_vector._meta_row(_meta(1))    # noqa: SLF001
    for k in ("chunk_id", "code", "company", "year", "page_no", "section"):
        assert k in row
    # 正文刻意不存（只有 BM25 索引那一份是唯一来源）
    assert "text" not in row


def test_load_missing_files_raise_readable_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VECTOR_DIR", tmp_path)
    monkeypatch.setattr(index_vector, "VECTORS_PATH", tmp_path / "vectors.npy")
    monkeypatch.setattr(index_vector, "META_PATH", tmp_path / "meta.jsonl")
    monkeypatch.setattr(index_vector, "MANIFEST_PATH", tmp_path / "manifest.json")
    with pytest.raises(FileNotFoundError, match="向量库不存在"):
        index_vector.load()
    # 有矩阵没 manifest：也要拒绝（半成品库比没库更危险）
    np.save(tmp_path / "vectors.npy", np.zeros((1, 2), dtype=np.float32))
    with pytest.raises(FileNotFoundError, match="manifest"):
        index_vector.load()


def test_verify_against_model_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(index_vector, "MANIFEST_PATH", tmp_path / "manifest.json")
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"model": "bge-small-zh", "dim": 512}), encoding="utf-8")
    ok, why = index_vector.verify_against({"model": "text-embedding-v4"})
    assert ok is False and "必须重建" in why
    ok2, _ = index_vector.verify_against({"model": "bge-small-zh"})
    assert ok2 is True


# ---------------- embedding 层 ----------------

def test_embed_texts_empty_returns_empty_matrix(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_BACKEND", "none")
    assert embed_texts([]).shape == (0, 0)


def test_none_backend_is_explicitly_unavailable(monkeypatch):
    """`none` 是为了验证"降级到纯 BM25 也能跑"而存在的通道，不是故障。"""
    monkeypatch.setattr(config, "EMBEDDING_BACKEND", "none")
    ok, why = embedding_ready()
    assert ok is False and "已显式禁用" in why
    with pytest.raises(EmbeddingUnavailable):
        embed_texts(["甲"])


def test_unknown_backend_raises_with_options(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_BACKEND", "openai")
    with pytest.raises(EmbeddingUnavailable, match="未知的 EMBEDDING_BACKEND"):
        active_spec()


def test_describe_never_leaks_key(monkeypatch):
    """描述接口会进 /api/health，绝不能回显 Key 本身。"""
    monkeypatch.setattr(config, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(config, "LLM_PROVIDERS", {"qwen": {
        "api_key": "sk-secret-1234567890", "base_url": "http://x", "models": ["m"]}})
    d = describe()
    assert "sk-secret" not in json.dumps(d, ensure_ascii=False)
    assert d["key_configured"] is True and d["ready"] is True


def test_api_without_key_reports_actionable_reason(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(config, "LLM_PROVIDERS", {"qwen": {
        "api_key": "", "base_url": "http://x", "models": ["m"]}})
    ok, why = embedding_ready()
    assert ok is False and "未配置 API Key" in why


def test_embeddings_response_is_sorted_by_index(monkeypatch):
    """返回必须**按 `index` 字段重排**：兼容端点里顺序错位的实现是存在的，
    一旦错位，向量就会与文本整片错配（检索结果看起来仍完全正常）。"""
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]}).encode()

    monkeypatch.setattr(config, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(config, "EMBEDDING_INTERVAL", 0)
    monkeypatch.setattr(config, "LLM_PROVIDERS", {"qwen": {
        "api_key": "k", "base_url": "http://x/v1", "models": ["m"]}})
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())

    mat = embed_texts(["第一段", "第二段"])
    assert mat[0].tolist() == [1.0, 0.0]      # index=0 的向量必须落在第 0 行
    assert mat[1].tolist() == [0.0, 1.0]


def test_embeddings_count_mismatch_raises(monkeypatch):
    """返回条数不符必须报错：少一条就意味着"向量与文本错位"，宁可整批失败。"""
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()

    monkeypatch.setattr(config, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(config, "EMBEDDING_INTERVAL", 0)
    monkeypatch.setattr(config, "LLM_PROVIDERS", {"qwen": {
        "api_key": "k", "base_url": "http://x/v1", "models": ["m"]}})
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    with pytest.raises(EmbeddingUnavailable, match="条数不符"):
        embed_texts(["甲", "乙"])
