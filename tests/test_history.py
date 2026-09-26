"""多轮指代消解用例（纯函数，不联网）。

守的纪律与 `filters.detect` 一脉相承：**拿不准就不补，绝不猜**。
多轮的诱惑是"上一轮问的是茅台，这轮的'它'自然还是茅台"——但一旦上一轮是对比题
（多家公司），或本轮已经显式点名了另一家，沿用就会把答案引到错误的公司上，
而且从结果上完全看不出来。所以本组用例既要有"该沿用的要沿用"，
更要有"**本轮显式实体优先**""**不唯一/为空时不补**""**年份不跨公司沿用**"。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src import config, llm, streaming
from src.graph import builder
from src.graph.state import initial_state
from src.retrieve import filters


@pytest.fixture(autouse=True)
def _watchlist(monkeypatch):
    """固定一份 watchlist，让断言不依赖 config/watchlist.yaml 的真实内容。"""
    rows = [
        {"code": "600519", "name": "贵州茅台", "industry": "食品饮料", "years": [2024, 2025]},
        {"code": "000858", "name": "五粮液", "industry": "食品饮料", "years": [2024]},
        {"code": "300750", "name": "宁德时代", "industry": "电力设备", "years": [2024]},
        {"code": "601318", "name": "中国平安", "industry": "非银金融", "years": [2024]},
    ]
    monkeypatch.setattr(config, "load_watchlist", lambda: rows)
    filters.reset_cache()
    yield
    filters.reset_cache()


def _round(question: str, *, code=None, year=None, intent="rag", **extra) -> dict:
    """一轮对话的实体记录（`history` 的单项形状）。"""
    return {"question": question, "intent": intent, "code": code, "year": year, **extra}


# 上一轮：贵州茅台 2024 年
_MAOTAI = [_round("贵州茅台2024年的毛利率是多少", code="600519", year=2024)]


# ---------------- ① state 接入 history ----------------

def test_initial_state_accepts_history_and_defaults_empty():
    st = initial_state("它的净利率是多少", history=_MAOTAI)
    assert isinstance(st["history"], list) and len(st["history"]) == 1
    assert st["history"][0] == {"question": "贵州茅台2024年的毛利率是多少",
                                "intent": "rag", "code": "600519", "year": 2024}
    # 缺省（不传）时是空列表，而不是 None —— 下游一律当列表用，免得每处都判空
    assert initial_state("它的净利率是多少")["history"] == []


def test_to_response_does_not_export_history():
    """history 只是图内上下文；导出会让响应体随轮次线性膨胀，而客户端并不需要它。"""
    from src.graph.state import to_response

    resp = to_response(initial_state("问题", history=_MAOTAI))
    assert "history" not in resp


# ---------------- ② 本轮无公司 → 沿用上一轮 ----------------

def test_carry_company_from_recent_round():
    d = filters.resolve_entities("它的净利率是多少", history=_MAOTAI)
    assert d["code"] == "600519"
    assert "沿用上一轮的公司" in d["note"]
    assert "贵州茅台" in d["note"]
    assert d["sources"]["company"] == "history"


# ---------------- ③ 本轮显式公司优先，绝不覆盖 ----------------

def test_explicit_company_never_overridden_by_history():
    d = filters.resolve_entities("五粮液的最新营收是多少", history=_MAOTAI)
    assert d["code"] == "000858"
    assert d["sources"]["company"] == "current"
    assert "沿用上一轮的公司" not in (d["note"] or "")


# ---------------- ④ 不唯一 / 为空 → 不补 ----------------

def test_no_carry_when_history_company_empty():
    d = filters.resolve_entities(
        "它的净利率是多少", history=[_round("上轮没点名公司", code=None, year=None)])
    assert d["code"] is None
    assert "沿用" not in (d["note"] or "")


def test_no_carry_when_history_company_ambiguous():
    """上一轮是"多家对比" → 没有唯一公司可用，宁愿不猜。"""
    d = filters.resolve_entities(
        "它的净利率是多少",
        history=[_round("茅台和五粮液2024年的营收对比", codes=["600519", "000858"], year=2024)])
    assert d["code"] is None
    assert "沿用" not in (d["note"] or "")


def test_no_history_no_carry():
    assert filters.resolve_entities("它的净利率是多少")["code"] is None
    assert filters.resolve_entities("它的净利率是多少", history=[])["code"] is None


# ---------------- ⑤ 年份同理，但不跨公司 ----------------

def test_year_carried_along_with_company_marks_both_sources():
    # 公司与年份都来自上一轮（同一家公司）→ 两者都要标明来源
    d = filters.resolve_entities("它的净利率是多少", history=_MAOTAI)
    assert (d["code"], d["year"]) == ("600519", 2024)
    assert d["sources"] == {"company": "history", "year": "history"}
    assert "沿用上一轮的公司" in d["note"] and "沿用上一轮的年份" in d["note"]


def test_year_carried_when_explicit_company_matches_history_marks_both_sources():
    # 公司来自本轮、年份来自上一轮 → 同为一家公司，允许沿用；两个来源都标明
    d = filters.resolve_entities("茅台的最新净利率", history=_MAOTAI)
    assert d["code"] == "600519" and d["year"] == 2024
    assert d["sources"] == {"company": "current", "year": "history"}
    assert "沿用上一轮的年份" in d["note"]


def test_year_not_carried_across_companies():
    # 本轮点了另一家（五粮液）→ 上一轮（茅台）的年份**不得**跨公司沿用
    d = filters.resolve_entities("五粮液的最新营收", history=_MAOTAI)
    assert d["code"] == "000858"
    assert d["year"] is None and d["sources"]["year"] is None
    assert "沿用上一轮的年份" not in (d["note"] or "")


def test_current_year_wins_over_history():
    d = filters.resolve_entities("五粮液2023年的营收", history=_MAOTAI)
    assert d["year"] == 2023 and d["sources"]["year"] == "current"


# ---------------- 上限裁剪 ----------------

def test_trim_history_keeps_most_recent_rounds(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_MAX", 2)
    hist = [_round(f"第{i}轮", code="600519", year=2024) for i in range(5)]
    kept = filters.trim_history(hist)
    assert [r["question"] for r in kept] == ["第3轮", "第4轮"]


def test_initial_state_trims_history(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_MAX", 1)
    hist = [_round("第0轮", code="600519"), _round("第1轮", code="600519")]
    st = initial_state("问题", history=hist)
    assert [r["question"] for r in st["history"]] == ["第1轮"]


# ---------------- SSE 端到端：流式路径也要能落库、能累积多轮 ----------------
#
# 前端走的是 `/api/ask/stream`（SSE）。流式路径是**手工跑节点**的，不走 `graph.invoke`，
# 所以它曾经**从不写 Checkpointer** —— 于是纯 SSE 会话里：多轮指代消解失效
# （`load_history` 读不回），批次 B 的引用卡片（`GET /api/citations` → `agent_status`）
# 也会 404。本组用例把这条缺口钉死。

_INDEX = Path(__file__).resolve().parent.parent / "data" / "index" / "bm25.pkl"


@pytest.fixture
def sse_session(tmp_path, monkeypatch):
    """隔离 Checkpointer 与审计库，并把模型固定为"未就绪"（走**离线**降级路径）。

    `llm.is_ready = (False, ...)` 保证 `_rag_events` 不会去调 `_astream_llm`，
    因此这两条用例**不联网、不花 token**。
    """
    from src import db
    from src.graph import checkpoint as ckpt

    monkeypatch.setattr(config, "CHECKPOINT_DB_PATH", tmp_path / "ckpt.db")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(config, "HITL_ENABLED", False)
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "")
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "未配置 API Key"))
    db.init_schema(db_path=tmp_path / "audit.db")
    ckpt.reset()
    yield
    ckpt.reset()


def _collect(gen) -> list[tuple[str, dict]]:
    async def run():
        return [event async for event in gen]
    return asyncio.run(run())


def _done(events: list[tuple[str, dict]]) -> dict:
    return dict(events)["done"]["response"]


@pytest.mark.skipif(not _INDEX.exists(), reason="本地无 bm25 索引，跳过需要真索引的 SSE 用例")
def test_sse_round_persists_and_is_readable(sse_session):
    """SSE 跑完一轮后会话可被读回，且**落库态与 `done` 响应同口径**。"""
    from src import audit as audit_mod

    tid = "sse-persist-1"
    # `force_intent="rag"`：这句话本身会被路由判为数值题（走工具层、不检索），
    # 这里要验的是**检索层的实体沿用 + 流式落库**，所以显式走问答链路。
    events = _collect(streaming.stream_answer(
        "贵州茅台2024年的毛利率是多少", thread_id=tid, mode="bm25", force_intent="rag"))
    resp = _done(events)
    assert resp["retrieval"]["filters"]["code"] == "600519", "真索引下应识别出茅台"

    st = builder.agent_status(tid)
    assert st["found"] is True, "流式定稿必须落 Checkpointer，否则 citations 会 404"
    # 这句话在离线降级下会命中 `citation_unsupported`（摘录里含无出处的数字）→ **合法挂起**；
    # 一次性路径（`run_agent`）对同一问题同样挂起，所以这里要比的是"两条路同口径"，
    # 而不是硬写 False —— 硬写 False 只在"挂起态根本没落库"这个缺陷存在时才成立。
    assert st["pending"] is resp["hitl"]["pending"], "落库态必须与 done 响应同口径"
    assert st["response"]["answer"] == resp["answer"]
    assert st["response"]["citations"] == resp["citations"]

    # 审计口径与 /api/ask 一致：流式这条也要留痕
    rows = audit_mod.recent(5)
    assert any(r.get("action") == "ask" and (r.get("detail") or {}).get("thread_id") == tid
               for r in rows), "流式问答必须与 /api/ask 同口径写审计"


@pytest.mark.skipif(not _INDEX.exists(), reason="本地无 bm25 索引，跳过需要真索引的 SSE 用例")
def test_sse_second_round_resolves_pronoun(sse_session):
    """第一轮点名茅台、第二轮只问「它」—— 纯 SSE 也要能靠上一轮沿用同一家。"""
    tid = "sse-multi-2"

    r1 = _done(_collect(streaming.stream_answer(
        "贵州茅台2024年的毛利率是多少", thread_id=tid, mode="bm25", force_intent="rag")))
    assert r1["retrieval"]["filters"]["code"] == "600519"

    history = builder.load_history(tid)
    assert [h["code"] for h in history] == ["600519"], "上一轮实体要能从库里读回"

    r2 = _done(_collect(streaming.stream_answer(
        "它的毛利率是多少", thread_id=tid, mode="bm25", force_intent="rag",
        history=history)))

    # 第二轮问句**不含公司名** → 只能靠沿用；可核对：过滤条件 + 每条引用都指向茅台
    assert r2["retrieval"]["filters"]["code"] == "600519"
    assert r2["retrieval"]["filters"]["year"] == 2024
    assert r2["citations"] and all(c["code"] == "600519" for c in r2["citations"])
    assert all(c["company"] == "贵州茅台" for c in r2["citations"])

    # 纯 SSE 也要能**累积** history（第一轮 + 第二轮）
    assert len(builder.load_history(tid)) == 2