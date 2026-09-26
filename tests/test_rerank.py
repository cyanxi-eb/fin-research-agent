"""重排用例（纯内存、**不联网** —— API 通道用替身打桩）。

重排的代码路径有一个特点：**成功路径很短，失败路径很多**（没 Key、网络错、
服务端 4xx、返回条数不符、候选缺正文……）。而这些失败路径的处理规则完全一致：
**降级、保留候选、把原因写进 note**。所以用例的重点全在失败路径上 ——
一条"重排挂了就整题答不出来"的回归，用户可能几周都遇不到，但一旦遇到就是事故。
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from src import config
from src.retrieve import rerank as rr


def _hits(n: int = 3, with_text: bool = True) -> list[dict]:
    out = []
    for i in range(n):
        out.append({"chunk_id": f"c{i}", "text": f"第{i}段正文" if with_text else "",
                    "score": 1.0 - i * 0.1, "citation": f"某公司2024年年报 P{i}"})
    return out


class _FakeResp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_passthrough_is_default_and_explicit(monkeypatch):
    """默认必须是直通：重排每次问答都要多花一次 API 调用，
    收益没有先用评测量化之前不该默认打开。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "passthrough")
    res = rr.rerank("毛利率", _hits())
    assert res["applied"] is False
    assert [h["chunk_id"] for h in res["hits"]] == ["c0", "c1", "c2"]
    assert "未启用重排" in res["note"]
    # 空候选不报错、也不给无意义的说明
    assert rr.rerank("毛利率", [])["note"] is None


def test_unknown_backend_degrades_with_message(monkeypatch):
    """填错的通道名要**显式**降级并说明，而不是静默当成直通。"""
    res = rr.rerank("q", _hits(), backend="openai-rerank")
    assert res["applied"] is False
    assert "未知的重排通道" in res["note"]


def test_api_reorders_by_score(monkeypatch):
    """正常路径：按服务端分数重排，并把分数写进 signals 与 rerank_score。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")
    monkeypatch.setattr(config, "RERANK_PROVIDER", "qwen")
    monkeypatch.setattr(rr, "_api_rerank", lambda q, d, s: [
        {"index": 2, "score": 0.9}, {"index": 0, "score": 0.3}, {"index": 1, "score": 0.1}])
    res = rr.rerank("毛利率", _hits())
    assert res["applied"] is True
    assert [h["chunk_id"] for h in res["hits"]] == ["c2", "c0", "c1"]
    assert res["hits"][0]["rerank_score"] == 0.9
    assert res["hits"][0]["signals"]["rerank"] == 0.9
    assert "已用 api 重排 3/3 条" in res["note"]


def test_api_maps_by_index_not_by_order(monkeypatch):
    """**必须按返回的 `index` 字段回映射**，不能假设"返回顺序=输入顺序"。

    假定顺序的话，一旦上游打乱返回，分数就会整片错配到别的片段上 ——
    而错配后的排序"看起来完全正常"（都是真片段，只是优先级反了），
    是最难被发现的一类错误。这里故意让服务端乱序返回。
    """
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")
    monkeypatch.setattr(rr, "_api_rerank", lambda q, d, s: [
        {"index": 1, "score": 0.9}, {"index": 2, "score": 0.5}, {"index": 0, "score": 0.1}])
    res = rr.rerank("毛利率", _hits())
    assert [h["chunk_id"] for h in res["hits"]] == ["c1", "c2", "c0"]


def test_partial_results_keep_unscored_candidates(monkeypatch):
    """服务端只回了一部分条目时，**未打分的候选要保留在末尾**，不能丢。

    悄悄少几条候选会让"为什么这题召回变差了"完全无法排查。
    """
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")
    monkeypatch.setattr(rr, "_api_rerank", lambda q, d, s: [{"index": 2, "score": 0.7}])
    res = rr.rerank("毛利率", _hits(3))
    assert [h["chunk_id"] for h in res["hits"]] == ["c2", "c0", "c1"]
    assert res["hits"][0].get("rerank_score") == 0.7
    assert "未被服务端打分" in res["note"]


def test_empty_api_result_degrades(monkeypatch):
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")
    monkeypatch.setattr(rr, "_api_rerank", lambda q, d, s: [])
    res = rr.rerank("毛利率", _hits())
    assert res["applied"] is False
    assert "返回空结果" in res["note"]


def test_http_error_degrades_with_server_detail(monkeypatch):
    """服务端报错要**把它的原话带上**（模型名错、超长、限流都会在 body 里说清楚）。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")

    def boom(q, d, s):
        raise urllib.error.HTTPError("u", 400, "Bad Request", {}, _Body(b'{"message":"model not found"}'))

    monkeypatch.setattr(rr, "_api_rerank", boom)
    res = rr.rerank("毛利率", _hits())
    assert res["applied"] is False
    assert "HTTP 400" in res["note"] and "model not found" in res["note"]
    assert len(res["hits"]) == 3        # 候选一条都不能少


class _Body:
    def __init__(self, b: bytes):
        self._b = b

    def read(self) -> bytes:
        return self._b

    # HTTPError 会把它当 file-like 用，退出时调 close；没有这个方法会在
    # 解释器 GC 阶段抛 PytestUnraisableExceptionWarning，噪声掩盖真问题
    def close(self) -> None:
        pass

    # HTTPError 会把它当 file-like 用，退出时调 close；没有这个方法会在
    # 解释器 GC 阶段抛 PytestUnraisableExceptionWarning，噪声掩盖真问题
    def close(self) -> None:
        pass


def test_arbitrary_exception_still_degrades(monkeypatch):
    """连非 HTTP 异常（超时、解析错）也必须降级 —— 重排绝不能让问答挂掉。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")

    def boom(q, d, s):
        raise TimeoutError("read timeout")

    monkeypatch.setattr(rr, "_api_rerank", boom)
    res = rr.rerank("毛利率", _hits())
    assert res["applied"] is False
    assert "TimeoutError" in res["note"]


def test_candidates_without_text_are_kept_but_flagged(monkeypatch):
    """缺正文的候选无法参与重排，但要保留并**在 note 里说出来**。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")
    monkeypatch.setattr(rr, "_api_rerank", lambda q, d, s: [
        {"index": 0, "score": 0.9}, {"index": 1, "score": 0.2}, {"index": 2, "score": 0.1}])
    hits = _hits(3)
    hits[1]["text"] = ""
    res = rr.rerank("毛利率", hits)
    assert "1 条候选无正文" in res["note"]
    assert {h["chunk_id"] for h in res["hits"]} == {"c0", "c1", "c2"}


def test_only_head_is_reranked(monkeypatch):
    """只重排前 RERANK_TOP_N 条（逐对计算，深层候选本来也进不了 topk），
    但**剩下的必须原序保留在结果里**。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")
    monkeypatch.setattr(config, "RERANK_TOP_N", 2)
    seen = {}

    def fake(q, docs, s):
        seen["n"] = len(docs)
        return [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}]

    monkeypatch.setattr(rr, "_api_rerank", fake)
    res = rr.rerank("毛利率", _hits(5))
    assert seen["n"] == 2
    assert len(res["hits"]) == 5
    assert [h["chunk_id"] for h in res["hits"]][:2] == ["c1", "c0"]


def test_long_doc_is_truncated_before_sending(monkeypatch):
    """超长正文要按配置截断再送 —— 整批 400 的服务端错误往往就是这里来的。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")
    monkeypatch.setattr(config, "RERANK_MAX_DOC_CHARS", 10)
    seen = {}

    def fake(q, docs, s):
        seen["lens"] = [len(d) for d in docs]
        return []

    monkeypatch.setattr(rr, "_api_rerank", fake)
    rr.rerank("毛利率", [{"chunk_id": "c0", "text": "字" * 500}])
    assert seen["lens"] == [10]


def test_status_reports_reason_without_key(monkeypatch):
    """没 Key 时 status 要给出**可操作的原因**，而且不算故障（直通是合法状态）。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "api")
    monkeypatch.setattr(config, "LLM_PROVIDERS",
                        {"qwen": {"api_key": "", "base_url": "http://x", "models": ["m"]}})
    st = rr.status()
    assert st["available"] is False
    assert "未配置 API Key" in st["reason"]

    monkeypatch.setattr(config, "RERANK_BACKEND", "nonsense")
    st2 = rr.status()
    assert st2["available"] is False and "未知的 RERANK_BACKEND" in st2["reason"]


def test_missing_text_file_import_does_not_break_passthrough(monkeypatch):
    """直通模式不应触碰任何外部依赖（sentence_transformers 没装也要能用）。"""
    monkeypatch.setattr(config, "RERANK_BACKEND", "passthrough")
    assert rr.rerank("q", _hits())["applied"] is False
    with pytest.raises(KeyError):
        rr.status()["nonexistent"]


def test_call_time_backend_overrides_config_for_status(monkeypatch):
    """调用时显式给的通道，必须**按它**判可用性，不能被配置值一票否决。

    这是 Step 4 收尾修掉的一个真 bug：`status()` / `_spec()` 只读 `config.RERANK_BACKEND`，
    于是评测里"开重排"那一档传了 `backend="api"`，可用性仍按配置（默认 `passthrough`）
    判成不可用 → 直接降级直通。后果是**报告上"开了重排"与"没开重排"两行数字完全一样**，
    看起来像"重排没用"，实际是它压根没被调用过 —— 一个"静默降级"吃掉了一整档消融。
    """
    monkeypatch.setattr(config, "RERANK_BACKEND", "passthrough")
    monkeypatch.setattr(config, "RERANK_PROVIDER", "qwen")
    # 显式塞一个假 Key，避免这条用例的结果取决于本机有没有配过真实密钥
    monkeypatch.setitem(config.LLM_PROVIDERS, "qwen",
                        {"api_key": "test-key", "base_url": "http://example.invalid"})
    monkeypatch.setattr(rr, "_api_rerank", lambda q, d, s: [
        {"index": 2, "score": 0.9}, {"index": 0, "score": 0.3}, {"index": 1, "score": 0.1}])

    # 配置是 passthrough，但调用方要 api → 必须真的重排
    res = rr.rerank("毛利率", _hits(), backend="api")
    assert res["applied"] is True, "配置值不该否决调用时显式指定的通道"
    assert [h["chunk_id"] for h in res["hits"]] == ["c2", "c0", "c1"]

    st = rr.status("api")
    assert st["backend"] == "api"
    assert "passthrough" not in st["reason"], "问 api 通道时不该拿配置里的 passthrough 来答"
    # 不传时就仍按配置（本用例配的是 passthrough）
    assert rr.status()["backend"] == "passthrough"
