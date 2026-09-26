"""主图编排用例：路由 → 三子图 → 校验 → HITL 挂起/恢复。

这批用例要守住的是**编排层**的三件事（子图内部各有自己的用例）：

1. **路由真的把问题送到该去的那条链路**（三条各一条，断言的是 `route.rule` 而不是
   只看 `intent` —— 后者可能是兜底得来的，两者意义完全不同）；
2. **拒答与故障不进 HITL**，只有三种原因才挂起；
3. **挂起的状态必须落在 Checkpointer 里**（把进程内的 saver 缓存清掉再读，
   就等于模拟"换了个进程"—— 这是 Step 5 验收标准的可自动化版本）。

用 `audit=False` 跑图：审计会写业务库，单测不该往真实库塞记录。
"""
from __future__ import annotations

import pytest

from src import config, llm
from src.graph import builder
from src.graph import checkpoint as ckpt
from src.retrieve import pipeline


# ==================== 夹具 ====================

@pytest.fixture
def tmp_checkpoint(tmp_path, monkeypatch):
    """把 Checkpointer 指到临时文件，并清掉进程内 saver 缓存。

    缓存必须清：`checkpoint._SAVERS` 是进程级单例，不清的话上一个用例的 saver
    会被这个用例复用，于是"换了库文件却读到旧数据"—— 用例之间互相污染。
    """
    monkeypatch.setattr(config, "CHECKPOINT_DB_PATH", tmp_path / "ckpt.db")
    ckpt.reset()
    yield
    ckpt.reset()


def _fake_retrieve(hits, **extra):
    def _fn(question, topk=None, code=None, year=None, mode=None, history=None):
        return {"ok": True, "hits": hits, "mode": mode or "bm25",
                "filters": {"code": code, "year": year}, "stats": {"max_score": 9.9},
                "absent_terms": [], "note": None, **extra}
    return _fn


def _hit(text, code="600519", company="贵州茅台", year=2024, page=32, section="公司治理"):
    return {"chunk_id": f"{code}-{year}-p{page}-1", "code": code, "company": company,
            "year": year, "report_type": "annual", "section": section, "page_no": page,
            "part": 1, "parts_total": 1, "text": text,
            "citation": f"{company}{year}年年报 P{page} {section}"}


@pytest.fixture
def no_llm(monkeypatch):
    """关掉模型：编排用例不该联网，也不该因为"有 Key 就改了行为"而结果飘。"""
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "单测强制离线"))


# ==================== 路由分发 ====================

def test_analysis_question_reaches_tool_layer(synth_db, tmp_checkpoint, no_llm):
    r = builder.run_agent("贵州茅台2024年的营业总收入是多少", audit=False)
    assert r["intent"] == "analysis"
    assert r["route"]["rule"] == "numeric_cue+indicator"
    assert r["tool_calls"], "数值题必须真的调了工具"
    assert r["verify"]["detail"]["intent"] == "analysis"
    assert r["hitl"]["pending"] is False
    assert r["thread_id"], "即使不挂起也要有 thread_id（审计与回溯都要用）"


def test_compliance_question_reaches_regulation_index(tmp_checkpoint, no_llm):
    if not __import__("src.retrieve.regulation", fromlist=["x"]).available():
        pytest.skip("法规索引未构建")
    r = builder.run_agent("上市公司未在规定期限内披露年度报告会有什么后果", audit=False)
    assert r["intent"] == "compliance"
    assert r["route"]["rule"] == "compliance_cue"
    assert r["citations"], "合规回答必须给条文级引用"
    assert all("条" in c["citation"] for c in r["citations"])
    assert r["verify"]["detail"]["from"] == "regulation_hits"


def test_document_question_reaches_rag_subgraph(tmp_checkpoint, no_llm, monkeypatch):
    hits = [_hit("公司建立了规范的治理结构，董事会下设审计委员会。")]
    monkeypatch.setattr(pipeline, "retrieve", _fake_retrieve(hits))
    r = builder.run_agent("公司治理结构是怎样的", audit=False)
    assert r["intent"] == "rag"
    assert r["route"]["rule"] == "default"
    assert r["citations"], "RAG 必须有引用"
    assert r["degraded"] is True, "无模型时应降级为原文摘录"
    assert r["cite_check"]["dangling"] == []


def test_force_intent_overrides_router_and_says_so(synth_db, tmp_checkpoint, no_llm):
    """强制意图要留下 `forced_by_request` 痕迹。

    否则事后看审计会以为这条是路由器判的 —— 而"人为指定"与"规则判定"
    的排查路径完全不同。
    """
    r = builder.run_agent("随便问点什么", force_intent="analysis", audit=False)
    assert r["intent"] == "analysis"
    assert r["route"]["rule"] == "forced_by_request"


# ==================== 拒答 / 故障不进 HITL ====================

def test_out_of_corpus_refusal_is_not_pending(tmp_checkpoint, no_llm, monkeypatch):
    """确定性拒答是**结论**，不是待确认 → 不挂起。

    把它塞进 HITL 会让待确认队列变成垃圾场，真正需要人看的反而被埋掉。
    """
    monkeypatch.setattr(pipeline, "retrieve",
                        _fake_retrieve([_hit("员工食堂采购台账")],
                                       absent_terms=["菜谱"]))
    r = builder.run_agent("公司食堂的菜谱是什么", audit=False)
    assert r["refused"] is True
    assert r["refusal_reason"] == "out_of_corpus"
    assert r["hitl"]["pending"] is False
    assert r["verify"]["checked"] is False


def test_retrieve_failure_is_not_pending(tmp_checkpoint, no_llm, monkeypatch):
    """检索故障是**系统状态**，不是待确认 —— 不挂起、不静默、如实报出错误码。

    注意问题必须是**会被路由到 rag** 的（"贵州茅台的毛利率是多少"会走 analysis，
    压根不经过检索 —— 拿它测这条会得到"通过了但什么都没测到"的假绿）。
    """
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: {
        "ok": False, "error": "index_missing", "message": "索引不存在",
        "hits": [], "mode": "bm25", "absent_terms": []})
    r = builder.run_agent("公司治理结构是怎样的", audit=False)
    assert r["intent"] == "rag", "这条用例的前提是走 RAG 链路"
    assert r["ok"] is False and r["error"] == "index_missing"
    assert r["hitl"]["pending"] is False, "检索故障是系统状态，不是待确认"


# ==================== HITL 挂起 / 跨进程恢复 ====================

def test_hitl_suspends_and_survives_restart(tmp_checkpoint, no_llm, monkeypatch):
    """验收标准：**进程 A 跑出挂起 → 进程 B 用同一 thread_id 读回并确认**。

    这里用"清掉进程内 saver 缓存"模拟换进程：状态只可能来自磁盘上的 Checkpointer。
    如果在内存里，清掉缓存后就什么都读不到 —— 那正是这条用例要拦住的情况。
    """
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "low_confidence")
    hits = [_hit("本报告期内公司治理结构未发生重大变化。")]
    monkeypatch.setattr(pipeline, "retrieve", _fake_retrieve(hits))

    r = builder.run_agent("公司治理结构是怎样的", audit=False)
    tid = r["thread_id"]
    assert r["hitl"]["pending"] is True
    assert r["hitl"]["reason"] == "low_confidence"
    assert r["ok"] is True, "挂起不是错误"

    ckpt.reset()                                   # ← 模拟"换了个进程"

    st = builder.agent_status(tid)
    assert st["found"] is True
    assert st["pending"] is True and st["next"] == ["hitl"]
    payload = st["payload"] or {}
    assert payload["reason"] == "low_confidence"
    assert payload["answer"], "挂起时要把答案一起存下来，人才能就着证据做决定"
    assert payload["thread_id"] == tid

    out = builder.resume_agent(tid, {"decision": "approve", "note": "已核对页码",
                                     "reviewer": "qa-01"}, audit=False)
    assert out["hitl"]["pending"] is False
    assert out["hitl"]["reviewed"] is True
    assert out["hitl"]["decision"]["decision"] == "approve"
    assert any("人工确认：approve" in n for n in out["notes"])

    with pytest.raises(ValueError):
        builder.resume_agent(tid, {"decision": "approve"}, audit=False)


def test_hitl_reject_marks_answer_as_not_published(tmp_checkpoint, no_llm, monkeypatch):
    """驳回不能"悄悄丢掉"：答案保留在审计里，但**显式标注不作为对外结论**。

    丢掉会让人以为从没问过；不标注则可能被当成已确认的结论引用出去。
    """
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "low_confidence")
    monkeypatch.setattr(pipeline, "retrieve",
                        _fake_retrieve([_hit("公司治理结构未发生重大变化。")]))
    r = builder.run_agent("公司治理结构是怎样的", audit=False)
    out = builder.resume_agent(r["thread_id"], {"decision": "reject", "note": "证据不足"},
                               audit=False)
    assert out["answer"].startswith("【人工复核未通过")


def test_no_pending_flow_status_reports_not_found(tmp_checkpoint):
    st = builder.agent_status("fa-does-not-exist")
    assert st["found"] is False and st["pending"] is False


# ==================== 两条入口同形 ====================

def test_run_agent_and_run_qa_share_shape(synth_db, tmp_checkpoint, no_llm, monkeypatch):
    """HTTP 面与脚本面不许漂移：两条入口返回的都是 `to_response` 的结果。"""
    monkeypatch.setattr(pipeline, "retrieve",
                        _fake_retrieve([_hit("公司治理结构稳定。")]))
    a = builder.run_agent("公司治理结构是怎样的", audit=False)
    b = builder.run_qa("公司治理结构是怎样的")
    assert set(a) == set(b), f"字段集不一致：{set(a) ^ set(b)}"
    assert a["route"]["rule"] == "default"
    assert b["route"]["rule"] == "forced_rag"


def test_thread_id_is_honoured_when_given(tmp_checkpoint, no_llm, monkeypatch):
    monkeypatch.setattr(config, "HITL_FORCE_REASON", "low_confidence")
    monkeypatch.setattr(pipeline, "retrieve", _fake_retrieve([_hit("内容。")]))
    r = builder.run_agent("公司治理结构是怎样的", thread_id="fa-fixed-001", audit=False)
    assert r["thread_id"] == "fa-fixed-001"
    assert builder.agent_status("fa-fixed-001")["found"] is True
