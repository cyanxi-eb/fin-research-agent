"""校验与 HITL 判定用例（纯函数 + 打桩，不跑图、不联网）。

`verify` 是"能不能对外发"的判据，所以它的失败方向必须成对覆盖：

- **该拦的必须拦**（答案里有编造的数字 / 引用死链 / 取数后库被改）→ 挂起；
- **不该拦的一个都不能拦**（拒答、故障、合规里的文号/条号）→ 不挂起。

第二类尤其重要：拒答与故障**不是待确认**，把它们塞进 HITL 会让待确认队列
变成垃圾场，真正需要人看的反而被埋掉。所以这里对"不挂起"的断言比"挂起"还多。
"""
from __future__ import annotations

from src import config
from src.graph import verify as vf
from src.tools import registry


def _state(**kw):
    base = {"question": "贵州茅台2024年的营业总收入是多少", "answer": "",
            "route": {"intent": "analysis"}, "citations": [], "hits": [],
            "tool_calls": [], "tool_results": [], "notes": [], "confidence": 0.9,
            "refused": False, "degraded": False, "thread_id": "t-1"}
    base.update(kw)
    return base


# ==================== 不该挂起的 ====================

def test_refused_is_not_pending():
    """确定性拒答是**结论**，不是待确认 → 不挂起，也不做校验。"""
    out = vf.verify_state(_state(refused=True, refusal_reason="low_coverage",
                                 answer="超出资料范围，不予回答。", confidence=0.0))
    assert out["hitl"]["pending"] is False
    assert out["verify"]["checked"] is False
    assert out["verify"]["supported"] is True


def test_error_is_not_pending():
    out = vf.verify_state(_state(error="retrieve_failed", answer="检索不可用"))
    assert out["hitl"]["pending"] is False
    assert out["verify"]["checked"] is False


def test_high_confidence_answer_is_not_pending():
    hits = [{"index": 1, "text": "本期营业总收入 1,020.00 亿元", "citation": "某公司2024年年报 P1"}]
    out = vf.verify_state(_state(
        route={"intent": "rag"}, answer="营业总收入为 1,020.00 亿元[1]",
        citations=[{"index": 1, "citation": "某公司2024年年报 P1"}], hits=hits,
        cite_check={"dangling": []}, confidence=0.9))
    assert out["hitl"]["pending"] is False, out["verify"]
    assert out["verify"]["supported"] is True


def test_degraded_answer_does_not_trigger_low_confidence():
    """降级摘录（没调模型）本身已明确标注，不该再进 HITL。

    降级是**已经告知用户**的状态：答案是原文摘录、没有推断。再挂起一次
    只是把"系统省了一次模型调用"变成"要人看一遍"，纯增负担。
    """
    out = vf.verify_state(_state(
        route={"intent": "rag"}, answer="【原文摘录】…[1]",
        citations=[{"index": 1}], hits=[{"text": "内容", "citation": "P1"}],
        degraded=True, confidence=0.2))
    assert out["hitl"]["pending"] is False


def test_compliance_doc_numbers_are_not_unsupported():
    """合规回答里的**文号与施行日期**必须能被解释（否则每条合规回答都白挂一次）。

    这是实测踩过的坑：`第182号` 与 `2025-07-01` 里的数字被判成"无出处"，
    于是每次合规问答都要人工确认 —— 闸门被噪音占满。
    """
    hits = [{"chunk_id": "xinpi-226#art21", "doc_no": "中国证监会令第226号",
             "effective_from": "2025-07-01", "article_no": 21,
             "citation": "《上市公司信息披露管理办法》（中国证监会令第226号）第二十一条",
             "text": "第二十一条　上市公司未在规定期限内披露年度报告…"}]
    out = vf.verify_state(_state(
        route={"intent": "compliance"},
        answer="[1] 《上市公司信息披露管理办法》（中国证监会令第226号）第二十一条\n"
               "    现行状态：current（自 2025-07-01 施行）",
        citations=[{"index": 1, "article_no": 21}],
        regulation_hits=hits, confidence=0.8))
    assert out["verify"]["unsupported"] == [], out["verify"]
    assert out["hitl"]["pending"] is False


# ==================== 该挂起的三种 ====================

def test_unsupported_number_triggers_citation_hitl():
    out = vf.verify_state(_state(
        answer="营业总收入为 1,020.00 亿元", tool_results=[
            {"ok": True, "series": [{"period": "2024-12-31", "value": 1.0e11,
                                     "display": "1,000.00 亿元"}]}],
        tool_calls=[], confidence=0.9))
    assert out["hitl"]["reason"] == vf.R_CITATION
    assert out["verify"]["supported"] is False
    assert [u["raw"] for u in out["verify"]["unsupported"]] == ["1,020.00"]


def test_dangling_citation_triggers_hitl():
    hits = [{"text": "营业总收入 1,020.00 亿元", "citation": "P1"}]
    out = vf.verify_state(_state(
        route={"intent": "rag"}, answer="营业总收入 1,020.00 亿元[3]",
        citations=[{"index": 1}], hits=hits, cite_check={"dangling": [3]}, confidence=0.9))
    assert out["hitl"]["reason"] == vf.R_CITATION
    assert out["verify"]["dangling_citations"] == [3]


def test_low_confidence_triggers_hitl():
    hits = [{"text": "营业总收入 1,020.00 亿元", "citation": "P1"}]
    out = vf.verify_state(_state(
        route={"intent": "rag"}, answer="大概有一亿多[1]",
        citations=[{"index": 1}], hits=hits, cite_check={"dangling": []},
        confidence=0.35))
    assert out["hitl"]["reason"] == vf.R_LOW_CONF


def test_numeric_mismatch_has_priority_over_everything(monkeypatch):
    """数据不一致优先级最高：取数之后库被改了，选哪个值都有风险 → 必须人工裁决。"""
    first = {"ok": True, "value": 1.0e11, "series": [{"value": 1.0e11}]}
    monkeypatch.setattr(registry, "dispatch",
                        lambda name, args: {"ok": True, "value": 9.9e11})
    out = vf.verify_state(_state(
        answer="营业总收入为 1,000.00 亿元",
        tool_calls=[{"name": "get_financial_indicator",
                     "arguments": {"company": "A", "indicator": "x"}}],
        tool_results=[first], confidence=0.9))
    assert out["hitl"]["reason"] == vf.R_NUMERIC
    assert out["verify"]["tool_drift"]


def test_recheck_skips_when_raw_payload_missing(monkeypatch):
    """拿不到原始 payload 时**不猜**：不能拿摘要凑一个指纹，那会稳定地"看起来一致"。

    用摘要比数值等于把这道闸门悄悄关掉 —— 这类"闸门失效但看着正常"的缺陷最难发现。
    """
    monkeypatch.setattr(registry, "dispatch",
                        lambda name, args: {"ok": True, "value": 999.0})
    out = vf.verify_state(_state(
        answer="营业总收入为 1,000.00 亿元",
        tool_calls=[{"name": "get_financial_indicator", "arguments": {}}],
        tool_results=[], confidence=0.9))
    assert out["verify"]["tool_drift"] == []


# ==================== 演练开关 ====================

def test_force_reason_only_overrides_pending_decision(monkeypatch):
    """演练开关只改"要不要挂起"，判定明细照实记录（不掩盖真问题）。"""
    monkeypatch.setattr(config, "HITL_FORCE_REASON", vf.R_LOW_CONF)
    hits = [{"text": "营业总收入 1,020.00 亿元", "citation": "P1"}]
    out = vf.verify_state(_state(
        route={"intent": "rag"}, answer="营业总收入 1,020.00 亿元[1]",
        citations=[{"index": 1}], hits=hits, cite_check={"dangling": []}, confidence=0.95))
    assert out["hitl"]["pending"] is True
    assert out["hitl"]["reason"] == vf.R_LOW_CONF
    assert out["verify"]["supported"] is True, "判定明细必须仍是真实结果"
    assert any("演练开关" in n for n in out["notes"])


def test_force_reason_does_not_mask_real_problem(monkeypatch):
    """已有真实问题（数字无出处）时，原因必须报真实的那一个。"""
    monkeypatch.setattr(config, "HITL_FORCE_REASON", vf.R_LOW_CONF)
    out = vf.verify_state(_state(answer="营业总收入为 1,020.00 亿元",
                                 tool_results=[{"value": 1.0e11}], confidence=0.9))
    assert out["hitl"]["reason"] == vf.R_CITATION
