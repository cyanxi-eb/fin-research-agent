"""RRF 融合用例（纯内存、不联网）。

守的是融合的**性质**而不是具体数值：RRF 的全部价值在于
"共识优先、量纲无关、结果确定"，这三条一旦坏掉，混合检索就退化成瞎拼。
"""
from __future__ import annotations

import pytest

import pytest

from src import config
from src.retrieve.fusion import explain, fuse


def _h(*ids: str) -> list[dict]:
    return [{"chunk_id": i} for i in ids]


def test_consensus_beats_single_route_first():
    """两路都靠前的候选，RRF 分必须高于只在单路排第一的候选。

    这是 RRF 的核心主张：**被两路同时认可**比"某一路给了最高分"更值得信任。
    """
    fused = fuse({"bm25": _h("A", "B", "C"), "vector": _h("C", "D")})
    assert fused[0]["chunk_id"] == "C"
    assert fused[0]["ranks"] == {"bm25": 3, "vector": 1}
    assert fused[0]["routes"] == ["bm25", "vector"]


def test_absent_route_is_not_penalized():
    """某路没召回 ≠ 排最后。把它当"最后一名"会系统性压低向量独有的片段
    —— 而那恰恰是加向量路想捞回来的东西。"""
    only_bm = fuse({"bm25": _h("A"), "vector": []})
    assert only_bm[0]["rrf_score"] == pytest.approx(1.0 / (config.RRF_K + 1))
    assert only_bm[0]["ranks"] == {"bm25": 1}     # 没有 vector 这个键，而不是 vector=999
    # 第二路完全空时，名次与"只跑一路"应当一致
    both = fuse({"bm25": _h("A"), "vector": _h("Z")})
    assert both[0]["chunk_id"] == "A"
    assert both[0]["rrf_score"] == pytest.approx(only_bm[0]["rrf_score"])


def test_rrf_score_formula_and_k():
    """`score = Σ 1/(k+rank)`，k 可配。名次从 1 开始（从 0 开始会让首名权重异常高）。"""
    fused = fuse({"bm25": _h("A", "B")}, k=59)
    assert fused[0]["rrf_score"] == pytest.approx(1 / 60)
    assert fused[1]["rrf_score"] == pytest.approx(1 / 61)


def test_weights_scale_each_route():
    """权重是**每路一个乘子**：调高某路权重只影响该路的贡献。"""
    plain = fuse({"bm25": _h("A"), "vector": _h("B")})
    weighted = fuse({"bm25": _h("A"), "vector": _h("B")}, weights={"bm25": 1.0, "vector": 3.0})
    pa = {i["chunk_id"]: i["rrf_score"] for i in plain}
    wa = {i["chunk_id"]: i["rrf_score"] for i in weighted}
    assert wa["B"] == pytest.approx(pa["B"] * 3)
    assert wa["A"] == pytest.approx(pa["A"])
    assert weighted[0]["chunk_id"] == "B"    # 权重生效后名次真的变了


def test_tie_break_is_deterministic():
    """同分时必须稳定排序（先比单路最好名次，再比 chunk_id）。

    评测要跑 Step3/Step4 两轮对比，排序若不确定，两次结果的差异就分不清
    是策略带来的还是随机的 —— 没有确定性的评测等于没有评测。
    """
    routes = {"bm25": _h("b", "a"), "vector": _h("a", "b")}
    first = [i["chunk_id"] for i in fuse(routes)]
    for _ in range(5):
        assert [i["chunk_id"] for i in fuse(routes)] == first
    # a 与 b 名次相同（1 与 2 互换），按 chunk_id 决出 a 在前
    assert first[0] == "a"


def test_drops_candidates_without_chunk_id():
    """没有 chunk_id 的候选无法去重、也无法回查正文，直接丢弃而不是当新片段。"""
    fused = fuse({"bm25": [{"chunk_id": "A"}, {"score": 1.0}, {"chunk_id": None}]})
    assert [i["chunk_id"] for i in fused] == ["A"]


def test_topk_and_empty_input():
    assert len(fuse({"bm25": _h(*"ABCDE")}, topk=2)) == 2
    assert fuse({"bm25": [], "vector": []}) == []
    assert fuse({}) == []


def test_explain_marks_consensus_and_single_route():
    routes = {"bm25": _h("A", "B"), "vector": _h("B")}
    fused = fuse(routes)
    text = "\n".join(explain(fused, routes))
    assert "两路共识" in text
    assert "仅bm25" in text
    assert "vector=—" in text          # 缺席的那一路显示成破折号而不是漏掉
