"""HTTP 面用例（TestClient，不联网）。

接口层最容易出的两类问题，这里各守一条：

1. **状态码语义错位**：拒答与挂起**都是 200**（它们不是错误），
   而参数非法是 400、没有挂起的流程可确认是 409。把拒答写成 4xx 会让前端
   不得不靠解析 body 来区分"错"与"答不了"，接错一次就长期错。
2. **两条入口（HTTP / 脚本）行为漂移**：`/api/ask` 必须与 `run_agent` 同形同值。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src import config, llm
from src.graph import checkpoint as ckpt
from src.retrieve import pipeline
from src.search import fetch as fetch_mod
from src.search import provider as provider_mod
from src.search import web_corpus
from src.search.provider import SearchResult
from src.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHECKPOINT_DB_PATH", tmp_path / "ckpt.db")
    ckpt.reset()
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "单测强制离线"))
    with TestClient(app) as c:
        yield c
    ckpt.reset()


def _hit(text="本报告期内公司治理结构未发生重大变化。"):
    return {"chunk_id": "600519-2024-p32-1", "code": "600519", "company": "贵州茅台",
            "year": 2024, "report_type": "annual", "section": "公司治理", "page_no": 32,
            "part": 1, "parts_total": 1, "text": text,
            "citation": "贵州茅台2024年年报 P32 公司治理"}


@pytest.fixture
def fake_rag(monkeypatch):
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: {
        "ok": True, "hits": [_hit()], "mode": "bm25", "filters": {},
        "stats": {"max_score": 9.0}, "absent_terms": [], "note": None})


# ==================== health ====================

def test_health_reports_every_degradable_channel(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    for key in ("index", "regulation", "llm", "vector", "rerank",
                "checkpointer", "hitl", "audit", "intents"):
        assert key in body, f"健康检查漏报 {key} —— 这类通道会静默降级，必须可见"
    assert body["version"].startswith("0.")
    assert set(body["intents"]) == {"rag", "analysis", "compliance"}


# ==================== 参数校验 ====================

def test_invalid_mode_and_intent_are_400(client):
    assert client.post("/api/ask", json={"question": "x", "mode": "bogus"}).status_code == 400
    assert client.post("/api/ask", json={"question": "x", "intent": "bogus"}).status_code == 400


def test_empty_question_is_rejected_by_schema(client):
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


# ==================== ask ====================

def test_ask_analysis_returns_tool_calls(synth_db, client):
    r = client.post("/api/ask", json={"question": "贵州茅台2024年的营业总收入是多少",
                                      "intent": "analysis"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "analysis" and body["route"]["rule"] == "forced_by_request"
    assert body["tool_calls"]
    assert "1,020.00 亿元" in body["answer"]


def test_refusal_is_200_not_4xx(client, fake_rag, monkeypatch):
    """拒答是**结论**，不是错误 → 必须 200 + refused=True。"""
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: {
        "ok": True, "hits": [_hit()], "mode": "bm25", "filters": {},
        "stats": {"max_score": 1.0}, "absent_terms": ["菜谱"], "note": None})
    r = client.post("/api/ask", json={"question": "公司食堂的菜谱是什么"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True and body["refusal_reason"] == "out_of_corpus"
    assert body["ok"] is True


def test_suspend_is_200_and_thread_id_is_returned(client, fake_rag, monkeypatch):
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "low_confidence")
    r = client.post("/api/ask", json={"question": "公司治理结构是怎样的",
                                      "thread_id": "fa-http-001"})
    assert r.status_code == 200
    body = r.json()
    assert body["hitl"]["pending"] is True
    assert body["thread_id"] == "fa-http-001"


# ==================== SSE 流式端点（/api/ask/stream）====================

def _first_sse_event(text: str) -> tuple[str, dict]:
    """解出 SSE 流里的第一个事件（`event:` 行 + `data:` 行）。"""
    block = text.split("\n\n", 1)[0]
    name, data = "", {}
    for line in block.splitlines():
        if line.startswith("event: "):
            name = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
    return name, data


def test_stream_without_index_is_503(client, monkeypatch):
    """索引没建是**环境状态** → 503，且必须在首个事件之前返回（流一旦开始就改不了状态码）。"""
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: {
        "ok": False, "error": "index_missing", "mode": "bm25",
        "message": "索引不存在：data/index/bm25.pkl", "hits": [],
        "absent_terms": [], "stats": {}, "degraded": False})
    r = client.post("/api/ask/stream",
                    json={"question": "公司治理结构是怎样的", "intent": "rag"})
    assert r.status_code == 503
    assert "索引" in r.json()["detail"]


def test_stream_first_event_is_meta(client, fake_rag):
    """正常时首个事件是 meta；头部必须禁缓存/禁代理缓冲，否则 SSE 会被攒起来再发。"""
    r = client.post("/api/ask/stream", json={"question": "公司治理结构是怎样的"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers.get("cache-control") == "no-cache"
    assert r.headers.get("x-accel-buffering") == "no"
    name, data = _first_sse_event(r.text)
    assert name == "meta"
    assert {"intent", "route", "thread_id"} <= set(data)


def test_stream_empty_question_is_422(client, fake_rag):
    """空问题复用 `AskRequest` 的 schema 约束（与 /api/ask 一致），不需要新字段。"""
    assert client.post("/api/ask/stream", json={"question": ""}).status_code == 422


# ==================== HITL 端点 ====================

def test_hitl_read_and_confirm_roundtrip(client, fake_rag, monkeypatch):
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "low_confidence")
    client.post("/api/ask", json={"question": "公司治理结构是怎样的",
                                  "thread_id": "fa-http-002"})

    st = client.get("/api/hitl/fa-http-002").json()
    assert st["pending"] is True and st["payload"]["reason"] == "low_confidence"

    ok = client.post("/api/hitl/fa-http-002/confirm",
                     json={"decision": "approve", "note": "已核对", "reviewer": "qa"})
    assert ok.status_code == 200
    assert ok.json()["hitl"]["reviewed"] is True

    # 已经确认过了 → 再确认是**状态冲突**（409），不是参数错（400）
    again = client.post("/api/hitl/fa-http-002/confirm", json={"decision": "approve"})
    assert again.status_code == 409


def test_hitl_unknown_thread_is_404_and_bad_decision_is_400(client):
    assert client.get("/api/hitl/nope").status_code == 404
    assert client.post("/api/hitl/nope/confirm",
                       json={"decision": "approve"}).status_code == 409
    assert client.post("/api/hitl/nope/confirm",
                       json={"decision": "maybe"}).status_code == 422  # Phase 3 消毒：枚举值在 schema 层拦


# ==================== 引用与审计 ====================

def test_citations_endpoint_returns_spec_and_session_citations(client, fake_rag):
    spec = client.get("/api/citations").json()
    assert "report_citation" in spec["spec"] and "regulation_citation" in spec["spec"]
    assert "numeric_source" in spec["spec"], "数值溯源的写法也要在口径里说明"

    client.post("/api/ask", json={"question": "公司治理结构是怎样的",
                                  "thread_id": "fa-cit-1"})
    got = client.get("/api/citations", params={"thread_id": "fa-cit-1"}).json()
    assert got["citations"], "会话里应能读回引用"
    assert got["citations"][0]["citation"].endswith("公司治理")


def test_audit_endpoint_records_ask(synth_db, client):
    client.post("/api/ask", json={"question": "贵州茅台2024年的营业总收入是多少",
                                 "intent": "analysis", "actor": "tester"})
    body = client.get("/api/audit").json()
    assert body["stats"]["rows"] >= 1
    assert any(r["action"] == "ask" for r in body["rows"])
    top = body["rows"][0]
    assert top["actor"] == "tester"
    assert top["detail"]["intent"] == "analysis"
    assert top["detail"]["route"]["rule"] == "forced_by_request"


# ==================== 联网搜索：健康状态 / 独立语料 / 手动触发 ====================

WEB_CLAIM = "贵州茅台2024年的营业总收入是多少"


def _wresult(domain: str, text: str, *, path: str = "/a", title: str = "财报解读"):
    """造一条**已带正文**的搜索结果（正文已灌好 ⇒ 交叉验证这一步不联网）。"""
    return SearchResult(title=title, url=f"https://{domain}{path}",
                        snippet=text[:80], source_name=domain,
                        fetched_at="2026-09-23 15:04", text=text)


@pytest.fixture
def web_env(tmp_path, monkeypatch):
    """把网络语料区、独立索引、配额文件全钉到临时目录（不碰开发库）。"""
    monkeypatch.setattr(config, "WEB_CORPUS_DIR", tmp_path / "web_corpus")
    monkeypatch.setattr(config, "WEB_BM25_PATH", tmp_path / "index" / "bm25_web.pkl")
    monkeypatch.setattr(config, "WEB_SEARCH_QUOTA_PATH", tmp_path / "quota.json")
    return tmp_path


def test_health_reports_web_channel(client, monkeypatch):
    """联网兜底也要在健康检查里可见，**尤其"开关开着但通道不可用"**这种静默降级形态。"""
    body = client.get("/api/health").json()
    web = body["web"]
    for key in ("enabled", "backend", "provider_available", "corpus_rows",
                "corpus_domains", "last_ingest_at", "quota"):
        assert key in web, f"健康检查漏报 web.{key} —— 这条通道会静默降级，必须可见"
    assert web["provider_available"] is True
    assert {"date", "used", "limit", "remaining"} <= set(web["quota"])

    # 指定 tavily 却没配 Key：**开关仍开着，但 provider_available 必须变 false**
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", True)
    monkeypatch.setattr(config, "WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "")
    web2 = client.get("/api/health").json()["web"]
    assert web2["enabled"] is True and web2["provider_available"] is False
    assert "TAVILY_API_KEY" in (web2["provider_reason"] or "")


def test_web_corpus_endpoint_shows_ingested_rows(client, web_env):
    """空语料区**不是错误**（200 + total=0）；入库后能读到，且条目是网络口径。"""
    empty = client.get("/api/web/corpus").json()
    assert empty["total"] == 0 and empty["rows"] == []

    web_corpus.ingest([_wresult("news.com", "营业总收入为 1,020.00 亿元。")],
                      status="consistent", query=WEB_CLAIM)
    got = client.get("/api/web/corpus", params={"limit": 5}).json()
    assert got["total"] == 1 and got["stats"]["domains"] == 1
    row = got["rows"][0]
    assert row["url"].startswith("https://") and row["fetched_at"]
    assert "page_no" not in row and "section" not in row, "网络条目不得带年报引用字段"


def test_web_search_endpoint_respects_the_kill_switch(client, monkeypatch):
    """总开关关闭时 `ok=false` 且**一个请求都不发** —— 能被 GET 绕过的开关不算开关。"""
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", False)
    touched = []
    monkeypatch.setattr(provider_mod, "get_provider",
                        lambda *a, **k: touched.append("provider"))
    got = client.get("/api/web/search", params={"q": WEB_CLAIM}).json()
    assert got["ok"] is False and got["enabled"] is False
    assert got["corpus_cache"] is False and got["ingested"] is False
    assert touched == [], "开关关闭时不得触碰搜索通道"
    assert "总开关" in got["note"]


def test_web_search_endpoint_reuses_the_node_path(client, web_env, monkeypatch):
    """手动触发必须与自动兜底**同一条路径**：一致就入库，第二次提问改走缓存不再联网。"""
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", True)
    body = "贵州茅台2024年营业总收入为 1,020.00 亿元，同比增长。"
    fake = [_wresult("news.com", body, path="/a"),
            _wresult("finance.cn", body, path="/b")]
    monkeypatch.setattr(provider_mod, "get_provider", lambda *a, **k: type(
        "P", (), {"name": "fake", "search": lambda self, q, **k: fake})())
    monkeypatch.setattr(fetch_mod, "fetch_texts", lambda results, **k: [])

    got = client.get("/api/web/search", params={"q": WEB_CLAIM}).json()
    assert got["ok"] is True and got["provider"] == "fake"
    assert got["cross_validation"]["status"] == "consistent"
    assert got["cross_validation"]["distinct_domains"] == 2
    assert got["ingested"] is True and got["corpus_cache"] is False
    assert client.get("/api/web/corpus").json()["total"] == 2

    # 第二次同一个问题：命中的是刚入库的缓存，**不再联网**（通道被换成"碰了就报错"）
    def boom(*a, **k):
        raise AssertionError("缓存命中时不该再触碰搜索通道")

    monkeypatch.setattr(provider_mod, "get_provider", boom)
    again = client.get("/api/web/search", params={"q": WEB_CLAIM}).json()
    assert again["corpus_cache"] is True and again["provider"] is None
    assert again["results"] == [] and "缓存" in again["note"]


def test_web_search_endpoint_reports_conflict_without_ingesting(client, web_env, monkeypatch):
    """来源数字不一致 → `conflict`、**不入库**（否则错数字会被缓存下来长期命中）。"""
    monkeypatch.setattr(config, "WEB_SEARCH_ENABLED", True)
    fake = [_wresult("news.com", "营业总收入为 1,020.00 亿元。", path="/a"),
            _wresult("finance.cn", "营业总收入为 9,999.00 亿元。", path="/b")]
    monkeypatch.setattr(provider_mod, "get_provider", lambda *a, **k: type(
        "P", (), {"name": "fake", "search": lambda self, q, **k: fake})())
    monkeypatch.setattr(fetch_mod, "fetch_texts", lambda results, **k: [])

    got = client.get("/api/web/search", params={"q": WEB_CLAIM}).json()
    assert got["cross_validation"]["status"] == "conflict"
    assert got["ingested"] is False
    assert client.get("/api/web/corpus").json()["total"] == 0, "冲突的来源不得入库"


def test_citations_spec_documents_the_web_citation(client):
    """网络引用是**另一套口径**：必须在 `/api/citations` 里显式说明，避免与年报引用混排。"""
    spec = client.get("/api/citations").json()["spec"]
    wc = spec["web_citation"]
    assert set(wc["fields"]) == {"title", "url", "source_name", "fetched_at"}
    assert "page_no" not in wc["fields"] and "section" not in wc["fields"]
