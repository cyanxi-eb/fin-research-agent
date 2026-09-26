"""SSE 事件协议与流式合成的契约用例 —— Step 6 批次 A（A1）。

要守的是**「流式与一次性两条路的输出必须同形」**这件事：前端只写一套渲染。
所以断言都冲着"事件名序列 / 终态形状 / 降级可见性 / 环境故障的界线"去，而不是某段文案：

1. 正常 RAG：事件名必须是 `meta → token* → citations → verify → done`，
   且 `done.response` 与 `to_response()` 同形（键一个都不能少）；
2. 挂起时在 `done` 前多一个 `hitl` 事件；
3. 模型不可用：**只发一次** `token`（降级摘录）再 `done`，且 `done.response.degraded is True`；
4. 流式中途失败：以 `done` 收尾（**绝不静默中断**），降级并写出原因；
5. `meta` 必带 `intent` / `route` / `thread_id`。

全部离线：检索用 `pipeline.retrieve` 替身，模型用 `answer._astream_llm` 替身，
`llm.is_ready()` 用 monkeypatch 改就绪状态。**不联网、不调真模型、不花 token。**
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import answer, citation, config, llm, streaming
from src.retrieve import pipeline


# ==================== 夹具 ====================

@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    """流式定稿会**落 Checkpointer + 写审计**（多轮缺口修复），故每个用例都用临时库：
    既不污染开发库，也避免用例之间互相看见对方的会话 / 审计行。"""
    from src.graph import checkpoint as ckpt

    monkeypatch.setattr(config, "CHECKPOINT_DB_PATH", tmp_path / "ckpt.db")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "audit.db")
    ckpt.reset()
    yield
    ckpt.reset()


HIT_TEXT = "贵州茅台2024年年报披露，毛利率为 91.53%。"


def _hit(index: int = 1, *, code: str = "600519", company: str = "贵州茅台",
         year: int = 2024, page_no: int = 87,
         section: str = "管理层讨论与分析", text: str = HIT_TEXT) -> dict:
    """构造一条**检索层统一形状**的 hit（与 `pipeline.normalize_hit` 同形）。"""
    raw = {"company": company, "year": year, "report_type": "annual",
           "page_no": page_no, "section": section}
    return {
        "chunk_id": f"{code}-{year}-p{page_no}-{index}",
        "score": 9.0 - index, "signals": {"bm25": 9.0 - index},
        "code": code, "company": company, "year": year, "page_no": page_no,
        "section": section, "part": index, "parts_total": 1,
        "citation": citation.format_citation(raw), "text": text, "chars": len(text),
    }


@pytest.fixture
def fake_retrieve(monkeypatch):
    """把检索换成可控替身（返回两条可引用片段）。"""
    hits = [_hit(1), _hit(2, page_no=88)]
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: {
        "ok": True, "error": None, "mode": "bm25", "question": a[0] if a else "?",
        "filters": {}, "hits": hits, "absent_terms": [],
        "stats": {"returned": len(hits), "max_score": hits[0]["score"]},
        "note": None})
    return hits


@pytest.fixture
def offline_llm(monkeypatch):
    """HITL 与就绪状态都固定下来，让事件序列可预期。"""
    monkeypatch.setattr(config, "HITL_ENABLED", False)
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "")
    monkeypatch.setattr(llm, "is_ready", lambda: (True, "ok"))


def _collect(gen) -> list[tuple[str, dict]]:
    """把异步生成器跑成事件列表（用例是同步的，这里临时开一个事件循环）。"""
    async def run():
        return [event async for event in gen]
    return asyncio.run(run())


def _json_tokens(payload: dict, size: int = 4):
    """把模型应返回的 JSON 切成若干块 —— 模拟逐段流式。"""
    text = json.dumps(payload, ensure_ascii=False)
    async def gen(q, hits):
        for i in range(0, len(text), size):
            yield text[i:i + size]
    return gen, text


def _names(events) -> list[str]:
    return [name for name, _ in events]


# ==================== 正常 RAG：事件序列 + 终态同形 ====================

def test_rag_event_order_and_done_shape(fake_retrieve, offline_llm, monkeypatch):
    """正常 RAG：事件名序列固定，`done.response` 与 `to_response()` 同形。"""
    gen, text = _json_tokens(
        {"answer": "毛利率为 91.53%[1]。", "used_citations": [1], "insufficient": False})
    monkeypatch.setattr(answer, "_astream_llm", gen)

    events = _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少", force_intent="rag"))
    names = _names(events)

    assert names[0] == "meta"
    assert names[-1] == "done"
    assert names.count("token") > 1, "模型可用时必须真的逐段流式，而不是攒完一次发"
    assert names.index("citations") < names.index("verify") < names.index("done")
    assert "hitl" not in names, "未挂起时不该有 hitl 事件"
    assert set(names) <= set(streaming.EVENTS), "事件名必须落在 EVENTS 协议里"

    meta = dict(events)["meta"]
    assert {"intent", "route", "thread_id"} <= set(meta), "meta 必须带 intent/route/thread_id"
    assert meta["intent"] == "rag"

    # token 拼接应还原模型原文（终态以 done 为准，这是"防丢字"的前提）
    assert "".join(d["text"] for n, d in events if n == "token") == text

    done = dict(events)["done"]["response"]
    for key in ("ok", "intent", "route", "thread_id", "answer", "citations", "refused",
                "refusal_reason", "degraded", "confidence", "verify", "hitl",
                "notes", "disclaimer"):
        assert key in done, f"done.response 缺字段 {key} —— 与一次性路径不同形了"
    assert done["answer"] == "毛利率为 91.53%[1]。"
    assert [c["index"] for c in done["citations"]] == [1]
    assert done["degraded"] is False
    assert done["hitl"]["pending"] is False


def test_citations_event_matches_done_response(fake_retrieve, offline_llm, monkeypatch):
    """`citations` 事件与 `done.response.citations` 必须是同一份，不能各自算。"""
    gen, _ = _json_tokens(
        {"answer": "毛利率为 91.53%[1]。", "used_citations": [1], "insufficient": False})
    monkeypatch.setattr(answer, "_astream_llm", gen)

    events = _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少", force_intent="rag"))
    emitted = dict(events)["citations"]
    done = dict(events)["done"]["response"]
    assert emitted["citations"] == done["citations"]
    assert emitted["count"] == len(done["citations"])


# ==================== 挂起：hitl 在 done 之前 ====================

def test_pending_emits_hitl_before_done(fake_retrieve, monkeypatch):
    """挂起时事件序列为 meta → token* → citations → verify → **hitl** → done。"""
    monkeypatch.setattr(config, "HITL_ENABLED", True)
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "low_confidence")
    monkeypatch.setattr(llm, "is_ready", lambda: (True, "ok"))
    gen, _ = _json_tokens(
        {"answer": "毛利率为 91.53%[1]。", "used_citations": [1], "insufficient": False})
    monkeypatch.setattr(answer, "_astream_llm", gen)

    events = _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少", force_intent="rag"))
    names = _names(events)

    assert "hitl" in names
    assert names.index("verify") < names.index("hitl") < names.index("done")
    assert dict(events)["hitl"]["pending"] is True
    assert dict(events)["done"]["response"]["hitl"]["pending"] is True


def test_pending_persists_resumable_checkpoint(fake_retrieve, monkeypatch):
    """挂起时落库的必须是**可恢复的挂起点**，否则前端点「确认放行」只会拿到 409。

    这是浏览器实测（B6）抓到的真实缺陷：流式路径原先把挂起态也按「已完成」落库
    （`as_node="finalize"` → `next` 为空），而 `resume_agent` 见到空 `next` 就抛
    `ValueError` → HTTP 409 —— 表现为"前端弹了确认面板，后端说没有挂起的流程"。
    断言特意压在**可恢复性**上（`next` / `payload` / 真能 resume），而不只是"有没有写库"。
    """
    from src.graph import builder

    monkeypatch.setattr(config, "HITL_ENABLED", True)
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "low_confidence")
    monkeypatch.setattr(llm, "is_ready", lambda: (True, "ok"))
    gen, _ = _json_tokens(
        {"answer": "毛利率为 91.53%[1]。", "used_citations": [1], "insufficient": False})
    monkeypatch.setattr(answer, "_astream_llm", gen)

    tid = "sse-pending-1"
    events = _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少",
                                              force_intent="rag", thread_id=tid))
    assert dict(events)["done"]["response"]["hitl"]["pending"] is True

    st = builder.agent_status(tid)
    assert st["found"] is True, "挂起态也要落库，否则引用明细与确认都读不到"
    assert st["pending"] is True
    assert list(st["next"]) == ["hitl"], "必须停在 hitl 之前，才有可恢复的挂起点"
    assert (st["payload"] or {}).get("reason") == "low_confidence"

    final = builder.resume_agent(
        tid, {"decision": "approve", "note": "用例", "reviewer": "test"}, audit=False)
    assert final["hitl"]["pending"] is False
    assert final["hitl"]["reviewed"] is True
    assert any("人工确认：approve" in n for n in final["notes"]), "决定要留痕"


def test_pending_reject_prefix_applied_once(fake_retrieve, monkeypatch):
    """恢复后 `finalize` 只应跑一次：驳回前缀不能叠成两行。

    挂起态若存的是 `finalize` **之后**的状态，恢复时 `finalize` 会再跑一次，
    「不予发布」前缀就会叠两遍 —— 这正是 `persist_pending` 存 `verify` 态的原因。
    """
    from src.graph import builder

    monkeypatch.setattr(config, "HITL_ENABLED", True)
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "low_confidence")
    monkeypatch.setattr(llm, "is_ready", lambda: (True, "ok"))
    gen, _ = _json_tokens(
        {"answer": "毛利率为 91.53%[1]。", "used_citations": [1], "insufficient": False})
    monkeypatch.setattr(answer, "_astream_llm", gen)

    tid = "sse-pending-2"
    _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少",
                                     force_intent="rag", thread_id=tid))
    final = builder.resume_agent(
        tid, {"decision": "reject", "note": "证据不足", "reviewer": "test"}, audit=False)

    assert final["answer"].count("【人工复核未通过") == 1, "驳回前缀被重复追加了"


# ==================== 模型不可用：一次性 token + degraded ====================

def test_model_unavailable_sends_single_token_and_degrades(fake_retrieve, monkeypatch):
    """`llm.is_ready()[0] is False` 时必须**只发一次 token**（降级摘录）再 done。

    注意 `is_ready()` 返回的是 `(bool, reason)` **元组** —— 就绪判断必须取 `[0]`。
    """
    monkeypatch.setattr(config, "HITL_ENABLED", False)
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "未配置 API Key"))

    async def boom(q, hits):
        raise AssertionError("模型不可用时不该调用 _astream_llm")
        yield  # pragma: no cover —— 让它成为异步生成器
    monkeypatch.setattr(answer, "_astream_llm", boom)

    events = _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少", force_intent="rag"))
    names = _names(events)

    assert names == ["meta", "token", "citations", "verify", "done"], \
        "模型不可用时只发一次 token（降级摘录）"
    done = dict(events)["done"]["response"]
    assert done["degraded"] is True
    assert "原文摘录" in done["answer"]
    assert done["citations"], "降级路径也要给出处"
    assert any("未调用大模型" in n for n in done["notes"]), "必须说清为什么没走模型"


def test_use_llm_false_short_circuits_streaming(fake_retrieve, offline_llm, monkeypatch):
    """`use_llm=False`（离线演示）时即使模型就绪也不流式，直接给有出处的摘录。"""
    async def boom(q, hits):
        raise AssertionError("use_llm=False 时不该调用 _astream_llm")
        yield  # pragma: no cover
    monkeypatch.setattr(answer, "_astream_llm", boom)

    events = _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少",
                                              force_intent="rag", use_llm=False))
    assert _names(events).count("token") == 1
    assert dict(events)["done"]["response"]["degraded"] is True


# ==================== 流式失败：降级收尾，绝不静默中断 ====================

def test_stream_failure_degrades_and_still_ends_with_done(fake_retrieve, offline_llm, monkeypatch):
    """流到一半抛异常：必须以 `done` 收尾并降级，把原因写进 notes（绝不静默中断）。"""
    async def boom(q, hits):
        yield "毛利"                    # 已经发出了一段
        raise RuntimeError("connection reset")
    monkeypatch.setattr(answer, "_astream_llm", boom)

    events = _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少", force_intent="rag"))
    names = _names(events)

    assert names[-1] == "done", "失败也必须以 done 收尾"
    done = dict(events)["done"]["response"]
    assert done["degraded"] is True
    assert any("流式调用大模型失败" in n for n in done["notes"])
    assert done["citations"], "降级路径也要给出处"


# ==================== 非流式意图：不逐段发 token ====================

def test_non_rag_intent_is_not_streamed(monkeypatch):
    """数值/合规两条链路本就没有 token 可流：`meta` 后直接给 citations/verify/done。"""
    monkeypatch.setattr(config, "HITL_ENABLED", False)
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "offline"))

    async def boom(*a, **k):
        raise AssertionError("非 RAG 意图不该走 RAG 的检索")
        yield  # pragma: no cover
    monkeypatch.setattr(pipeline, "retrieve", boom)

    events = _collect(streaming.stream_answer("这家公司的信息披露是否合规",
                                              force_intent="compliance"))
    names = _names(events)
    assert names[0] == "meta"
    assert "token" not in names, "非 RAG 链路不逐段发 token"
    assert names[-1] == "done"
    assert dict(events)["meta"]["intent"] == "compliance"
    assert dict(events)["done"]["response"]["intent"] == "compliance"


# ==================== 联网兜底：只有真跑过才多一个 web 事件 ====================

WEB_CLAIM = "贵州茅台2024年的营业总收入是多少"


@pytest.fixture
def web_env(tmp_path, monkeypatch):
    """网络语料区 / 独立索引 / 配额文件全钉到临时目录，并把联网开关打开。"""
    monkeypatch.setattr(config, "WEB_CORPUS_DIR", tmp_path / "web_corpus")
    monkeypatch.setattr(config, "WEB_BM25_PATH", tmp_path / "index" / "bm25_web.pkl")
    monkeypatch.setattr(config, "WEB_SEARCH_QUOTA_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", True)
    return tmp_path


def _refusing_retrieve(monkeypatch):
    """让检索问出"语料外实词"（`out_of_corpus`）—— 这正是兜底该被触发的信号。"""
    hits = [_hit(1, text="公司设有员工食堂，为员工提供工作餐。")]
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: {
        "ok": True, "error": None, "mode": "bm25", "question": a[0] if a else "?",
        "filters": {}, "hits": hits, "absent_terms": ["菜谱"],
        "stats": {"returned": 1, "max_score": 1.0}, "note": None})
    return hits


def _wresult(domain: str, text: str, *, path: str = "/a"):
    from src.search.provider import SearchResult

    return SearchResult(title="财报解读", url=f"https://{domain}{path}",
                        snippet=text[:80], source_name=domain,
                        fetched_at="2026-09-23 15:04", text=text)


def test_close_switch_keeps_event_sequence_unchanged(offline_llm, monkeypatch):
    """`FA_WEB_SEARCH_ENABLED=0` 时事件序列与从前**逐字相同**，且 `web` 是 `None`。

    这是向后兼容的那一半：既有断言一条都不用改。`None` 表示"这次没跑" ——
    与"跑了但一条结果都没有"（`{}` 那种形态，本项目刻意不用）必须分得开。
    """
    _refusing_retrieve(monkeypatch)
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", False)

    events = _collect(streaming.stream_answer("公司食堂的菜谱是什么", force_intent="rag"))
    names = _names(events)
    assert names == ["meta", "token", "citations", "verify", "done"]
    done = dict(events)["done"]["response"]
    assert done["refused"] is True and done["refusal_reason"] == "out_of_corpus"
    assert done["web"] is None and done["web_citations"] is None


def test_web_event_fires_only_when_fallback_ran(offline_llm, web_env, monkeypatch):
    """开关打开且真兜底过：`verify` 与 `done` 之间多一个 `web` 事件，与终态同物。"""
    from src.search import fetch as fetch_mod
    from src.search import provider as provider_mod

    _refusing_retrieve(monkeypatch)
    body = "贵州茅台2024年营业总收入为 1,020.00 亿元，同比增长。"
    fake = [_wresult("news.com", body), _wresult("finance.cn", body, path="/b")]
    monkeypatch.setattr(provider_mod, "get_provider", lambda *a, **k: type(
        "P", (), {"name": "fake", "search": lambda self, q, **k: fake})())
    monkeypatch.setattr(fetch_mod, "fetch_texts", lambda results, **k: [])

    events = _collect(streaming.stream_answer(WEB_CLAIM, force_intent="rag",
                                              thread_id="sse-web-1"))
    names = _names(events)

    assert set(names) <= set(streaming.EVENTS)
    assert names.index("verify") < names.index("web") < names.index("done")
    payload = dict(events)["web"]
    done = dict(events)["done"]["response"]
    assert payload == done["web"], "事件载荷与终态必须是同一份，不能各自算"
    assert payload["source"] == "live" and payload["provider"] == "fake"
    assert payload["cross_validation"]["status"] == "consistent"
    assert payload["ingested"] is True

    web_cites = done["web_citations"]
    assert len(web_cites) == 2, "每个独立域名一条"
    assert all(c["url"].startswith("https://") and c["fetched_at"] for c in web_cites)
    assert all("page_no" not in c and "section" not in c for c in web_cites), \
        "网络引用不得混入年报引用口径（页码 / 章节）"
    assert done["citations"] == [], "网络结果不进 `citations`：两套口径分开"


def test_web_event_is_absent_when_question_was_answered(fake_retrieve, offline_llm,
                                                        web_env, monkeypatch):
    """正常答出来（没拒答）时**不联网**：兜底只在"本地资料里没有"时才发生。"""
    from src.search import provider as provider_mod

    monkeypatch.setattr(provider_mod, "get_provider",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("正常回答时不该联网")))

    gen, _ = _json_tokens(
        {"answer": "毛利率为 91.53%[1]。", "used_citations": [1], "insufficient": False})
    monkeypatch.setattr(answer, "_astream_llm", gen)

    events = _collect(streaming.stream_answer("贵州茅台2024年的毛利率是多少",
                                              force_intent="rag"))
    assert "web" not in _names(events)
    assert dict(events)["done"]["response"]["web"] is None