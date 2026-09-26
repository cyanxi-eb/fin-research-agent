"""RAG 问答子图的三个节点：`retrieve` → `generate` → `cite`。

节点必须**薄**：逻辑都在 `src/retrieve/pipeline.py` 与 `src/answer.py` 里，
节点只负责"读写状态 + 兜住异常"。理由很实际 —— 节点里的逻辑只能通过跑图来测，
而图跑起来要先建索引、要联网调模型；把它留在纯函数里就能直接单测。
**可测性决定了逻辑该放在哪一层。**

本文件同时提供 Step 5 主图的编排节点（`router_node` / `verify_node` / `hitl_node` /
`finalize_node`）。它们同样薄：判定逻辑在 `router.py` / `verify.py`，
节点只做"状态进出 + 落 notes"。三条子图各自的节点在
`subgraph_analysis.py` / `subgraph_compliance.py` 与这里。
"""
from __future__ import annotations

import re

from src import answer as answer_mod
from src import config
from src.graph import router as router_mod
from src.graph import verify as verify_mod
from src.graph.state import QAState
from src.retrieve import pipeline

_CITE_RE = re.compile(r"\[(\d{1,2})\]")


def _cited_indices(text: str) -> list[int]:
    out: list[int] = []
    for m in _CITE_RE.finditer(text or ""):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
    return out


def retrieve_node(state: QAState) -> QAState:
    """检索节点：问题 → hits。

    检索失败（索引缺失等）在这里就分流掉，**不进 generate** ——
    让模型拿着一堆空上下文去编，是最糟的失败模式。
    """
    res = pipeline.retrieve(
        state.get("question", ""),
        topk=state.get("topk") or config.ANSWER_CANDIDATE_TOPK,
        code=state.get("code"), year=state.get("year"), mode=state.get("mode"),
        # 多轮上下文：让"它 / 该公司"这类问句能沿用上一轮的实体（见 filters.resolve_entities）
        history=state.get("history") or None)

    if not res.get("ok"):
        return {
            "hits": [], "retrieval": {"mode": res.get("mode"), "returned": 0},
            "error": res.get("error") or "retrieve_failed",
            "message": res.get("message"),
            "refused": True,
            "refusal_reason": res.get("error"),
            "answer": f"检索不可用：{res.get('message')}",
            "notes": (state.get("notes") or []) +
                     ["检索后端未就绪（这是系统状态，不是'没有数据'）。"],
            "disclaimer": config.ANSWER_DISCLAIMER,
        }

    return {
        "hits": res["hits"],
        "retrieval": {"mode": res.get("mode"), "filters": res.get("filters"),
                      "returned": len(res["hits"]),
                      "max_score": (res.get("stats") or {}).get("max_score"),
                      # 「语料外实词」要跟着状态走：拒答闸门在 generate 节点里用，
                      # 而 generate **不重新检索** —— 少带这一个字段，
                      # 走图调用就会比直接调用少一道闸门（行为漂移，且很难发现）。
                      "absent_terms": res.get("absent_terms") or []},
    }


def generate_node(state: QAState) -> QAState:
    """生成节点：hits → 答案 + 引用（内部含两道拒答闸门与降级路径）。

    直接复用 `answer.synthesize` —— **图里和直接调用走同一份实现**。
    如果这里另写一遍，两边行为迟早漂移，而漂移出来的差异最难查。
    """
    res = {"ok": True, "question": state.get("question", ""),
           "hits": state.get("hits") or [],
           "mode": (state.get("retrieval") or {}).get("mode"),
           "filters": (state.get("retrieval") or {}).get("filters"),
           "absent_terms": (state.get("retrieval") or {}).get("absent_terms") or [],
           "stats": {"returned": len(state.get("hits") or []),
                     "max_score": (state.get("retrieval") or {}).get("max_score")},
           "note": None}

    out = answer_mod.synthesize(state.get("question", ""), res,
                               use_llm=state.get("use_llm", True))
    return {
        "answer": out["answer"],
        "refused": out["refused"],
        "refusal_reason": out["refusal_reason"],
        "degraded": out["degraded"],
        "evidence": out["evidence"],
        "citations": out["citations"],
        "confidence": out["confidence"],
        "llm": out["llm"],
        "notes": (state.get("notes") or []) + (out.get("notes") or []),
        "disclaimer": out["disclaimer"],
    }


def cite_node(state: QAState) -> QAState:
    """引用节点：校验答案正文里的编号与 citations 列表是否自洽，并定稿。

    这是 Step 5 `verify.py`（回查引用是否真支持结论）的**最小版本**：
    完整版要去读 chunk 原文判"支持/不支持"，这里只做**一致性检查** ——
    正文里写了 `[3]` 但引用列表里没有 3，前端就会出现"点了没反应"的死链。
    成本极低、收益确定，所以放在 Step 3 先做掉。

    同时把 `disclaimer` 落到状态里（而不是让前端自己拼）——
    免责声明是**产品约束**，不能因为换前端就没了。
    """
    text = state.get("answer") or ""
    cites = state.get("citations") or []
    have = {c["index"] for c in cites}
    referenced = set(answer_mod._citations_from_answer_text(text))   # noqa: SLF001

    dangling = sorted(referenced - have)     # 正文引了但列表没有 → 死链
    unused = sorted(have - referenced)       # 列表有但正文没引 → 相关但未被采用的片段

    notes = list(state.get("notes") or [])
    if dangling:
        notes.append(f"答案正文出现了引用列表里没有的编号 {dangling} —— 角标点击无落点，"
                     f"转述时请忽略这些角标（生成阶段只保留了通过校验的编号）。")
    if unused:
        notes.append(f"另有 {unused} 条检索到的相关片段未被答案显式引用（可在引用列表里查看）。")

    # **不过滤 citations**：那些"未被正文显式引用"的片段同样是检索回来的真实证据，
    # 前端需要它们做"其他相关片段"展示。把它们删掉是在**丢证据**，
    # 而本项目的立场是宁可多给出处、由用户判断，也不要替用户隐藏来源。
    return {
        "citations": cites,
        "cite_check": {"dangling": dangling, "unused": unused,
                       "cited": sorted(referenced), "listed": sorted(have)},
        "notes": notes,
        "disclaimer": state.get("disclaimer") or config.ANSWER_DISCLAIMER,
    }


# ==================== Step 5 主图节点 ====================

def router_node(state: QAState) -> QAState:
    """意图路由（**不调模型**，规则优先，见 `router.py` 的说明）。

    路由结果连同 `rule`（命中了哪条规则）一起进状态与响应 ——
    出错时这一条是唯一能快速定位"是路由错了还是检索错了"的线索。

    支持 `force_intent` 显式指定（API / 回归测试用）。**仍然把 `rule` 标成
    `forced_by_request`**：否则事后看审计会以为这条是路由器判的，
    而"人为指定"与"规则判定"的排查路径完全不同。
    """
    forced = state.get("force_intent")
    if forced in router_mod.INTENTS:
        r = {"intent": forced, "rule": "forced_by_request", "confidence": 1.0,
             "matched": [], "reason": f"调用方显式指定走 {forced}（未经路由判定）"}
    else:
        r = router_mod.route(state.get("question", ""))
    return {
        "route": r,
        "intent": r["intent"],
        "notes": (state.get("notes") or []) + [f"路由：{r['intent']}（{r['rule']}）—— {r['reason']}"],
    }


def analysis_node(state: QAState) -> QAState:
    """数值分析子图（**强制走工具层**）。"""
    from src.graph.subgraph_analysis import analysis_question

    return analysis_question(state)


def compliance_node(state: QAState) -> QAState:
    """合规核查子图（只查法规库）。"""
    from src.graph.subgraph_compliance import compliance_question

    return compliance_question(state)


def verify_node(state: QAState) -> QAState:
    """校验节点：数字回归 + 引用完整性 + 数据一致性重查，并给出 HITL 判定。"""
    return verify_mod.verify_state(state)


def hitl_node(state: QAState) -> QAState:
    """人工确认节点：`interrupt()` 把当前进度**落盘后挂起**，等人来。

    挂起时存进 checkpoint 的是完整状态（问题 / 答案 / 校验明细 / 引用），
    恢复时人能直接看到证据 —— 而不是只收到一句"请确认"。所以 payload 里带上
    `answer` 与 `verify`：让"要不要放行"这个决定**不需要再去别处翻证据**。

    恢复后（`Command(resume=decision)`）把人的决定记进 `hitl.decision`，
    并在 notes 里留痕 —— 审计要能回答"这条是谁、什么时候、依据什么放行的"。
    """
    from langgraph.types import interrupt

    hitl = dict(state.get("hitl") or {})
    payload = {
        "thread_id": state.get("thread_id"),
        "reason": hitl.get("reason"),
        "question": state.get("question"),
        "intent": state.get("intent"),
        "answer": state.get("answer"),
        "confidence": state.get("confidence"),
        "verify": state.get("verify"),
        "citations": state.get("citations"),
        "tool_calls": state.get("tool_calls"),
        "notes": state.get("notes"),
        "ask": "请人工确认该答案是否可对外发布（approve / reject + 备注）。",
    }
    decision = interrupt(payload)

    reviewed = {"pending": False, "reason": hitl.get("reason"),
                "thread_id": state.get("thread_id"), "reviewed": True,
                "decision": decision}
    notes = list(state.get("notes") or [])
    if isinstance(decision, dict):
        verdict = decision.get("decision") or decision.get("verdict") or "unknown"
        notes.append(f"人工确认：{verdict}"
                     + (f"；备注：{decision.get('note')}" if decision.get("note") else ""))
        if verdict in ("reject", "rejected", "deny"):
            return {"hitl": reviewed, "notes": notes + [
                "人工判定**不予发布** —— 该答案保留在审计里，但不作为对外结论。"]}
    else:
        notes.append(f"人工确认结果：{decision!r}")
    return {"hitl": reviewed, "notes": notes}


def finalize_node(state: QAState) -> QAState:
    """定稿：统一补免责声明、跑一次引用一致性检查、把 HITL 决定落到对外文案。

    引用一致性检查为什么放在这里（而不是只放在 RAG 子图的 cite 节点）：
    三条子图都会产生 `[n]` 角标，任何一条出现"正文引了 [3] 但引用列表没有 3"
    都是同一类死链缺陷。定稿处统一做一次，等于**所有路径共用同一道检查**，
    不用依赖"每条子图都记得做"。
    """
    text = state.get("answer") or ""
    cites = state.get("citations") or []
    have = {c.get("index") for c in cites}
    referenced = set(_cited_indices(text))
    dangling = sorted(referenced - have)
    unused = sorted(have - referenced)

    notes = list(state.get("notes") or [])
    cite_check = dict(state.get("cite_check") or {})
    cite_check.update({"dangling": dangling, "unused": unused,
                       "cited": sorted(referenced), "listed": sorted(have)})
    if dangling:
        notes.append(f"定稿检查：正文出现引用列表里没有的编号 {dangling}（角标无落点）。")

    hitl = state.get("hitl") or {"pending": False}
    if hitl.get("reviewed") and isinstance(hitl.get("decision"), dict) \
            and (hitl["decision"].get("decision") in ("reject", "rejected", "deny")):
        text = ("【人工复核未通过，本条不作为对外结论】\n\n" + text)

    return {
        "answer": text,
        "cite_check": cite_check,
        "notes": notes,
        "disclaimer": state.get("disclaimer") or config.ANSWER_DISCLAIMER,
        "hitl": hitl,
    }
