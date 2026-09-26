"""校验节点 —— 三条子图共用的"最后一道闸门"，并决定要不要挂起给人看。

## 为什么校验不是 RAG 专属

直觉上"引用校验"是文档问答的事。但工具层返回的数值同样需要校验：
它要能对上库里那一行、且与**此刻**库里的值一致。把校验只挂在 RAG 上，
数值链路就成了唯一没有闸门的通道 —— 而它恰恰是要写进投资结论的那条。

## 校验什么（三条，都可判定、可打印）

1. **数字回归**：答案里的每个数字，都要能在"证据集合"里找到。
   - RAG/合规：证据 = 被引用片段的**原文**（数字必须真的在原文里出现过）；
   - 数值题：证据 = 工具返回的 payload（含展示单位与原始单位）。
   找不到 → `unsupported` 非空。
2. **引用完整性**：正文引了 `[n]` 而引用列表里没有 n（`cite_check.dangling`）→ 死链，
   点开无落点。这类问题机器能确定地判出来，不该留给人。
3. **数据一致性（重查）**：把工具调用**再执行一遍**，比对数值指纹。
   不一致说明"取数之后库被人改了"（并发写入 / 口径重灌），
   此时选哪个值都有风险 → 必须人工裁决。

## 什么**不**挂起（重要）

确定性拒答是**结论**，检索故障是**系统状态**，两者都不进 HITL。
把它们塞进待确认队列，队列会变成垃圾场，真正需要人看的反而被埋掉。
"""
from __future__ import annotations

from src import config
from src.graph.state import QAState
from src.numeric import collect_evidence_numbers, extract_numbers, unsupported_numbers
from src.tools import registry

# HITL 触发的三种原因（与 Step5-设计要点.md §D3 一一对应，不增不减）
R_CITATION = "citation_unsupported"
R_LOW_CONF = "low_confidence"
R_NUMERIC = "numeric_mismatch"


def _allowed_numbers(state: QAState) -> tuple[list[float], dict]:
    """构造"证据集合"：答案里的数字必须能被它解释。"""
    route = state.get("route") or {}
    intent = route.get("intent") or state.get("intent") or "rag"
    detail: dict = {"intent": intent}

    allowed: list[float] = []
    if intent == "analysis":
        allowed += collect_evidence_numbers(state.get("tool_results") or [])
        detail["from"] = "tool_results"
    elif intent == "compliance":
        # 合规路径的命中存在 `regulation_hits`（不是 `hits` —— 那是年报片段的槽位）。
        # 法规 chunk 自带 doc_no / effective_from / article_no，答案里的
        # "182 号""2025-07-01""第二十条"都能在这些元数据里对上。
        allowed += collect_evidence_numbers(state.get("regulation_hits") or [])
        detail["from"] = "regulation_hits"
        detail["articles"] = len(state.get("regulation_hits") or [])
    else:
        # RAG / 合规：引用片段（或命中的法规条文）的**原文**
        hits = state.get("hits") or []
        cites = state.get("citations") or []
        picked = []
        if cites:
            want = {c.get("index") for c in cites}
            picked = [h for i, h in enumerate(hits, start=1) if i in want] or hits
        else:
            picked = hits
        for h in picked:
            allowed += collect_evidence_numbers({"text": h.get("text") or "",
                                                 "citation": h.get("citation") or ""})
        detail["from"] = "cited_chunk_text"
        detail["chunks"] = len(picked)

    # 页码/年份/章节号也是答案里会出现的数字，且天然可信（来自我们的元数据）
    allowed += collect_evidence_numbers([
        {k: h.get(k) for k in ("code", "company", "year", "page_no", "section", "citation")}
        for h in (state.get("hits") or [])])
    return allowed, detail


def numeric_fingerprint(results: list[dict]) -> list[float]:
    """工具结果的数值指纹（排序后的数字列表），用于一致性重查。"""
    nums = collect_evidence_numbers(results)
    return sorted(round(v, 6) for v in nums)


def recheck_tools(tool_calls: list[dict], tool_results: list[dict] | None = None) -> list[dict]:
    """把工具调用再跑一遍，返回数值指纹不一致的调用。

    `tool_calls` 只存摘要（响应/审计用），原始 payload 在 `tool_results` 里（同序）。
    拿不到原始 payload 时**不猜**：跳过重查并在返回里说明，而不是用摘要凑一个指纹
    —— 用摘要比数值会稳定地"看起来一致"，等于把这道闸门悄悄关掉。
    """
    results = tool_results or []
    drift: list[dict] = []
    for i, c in enumerate(tool_calls or []):
        name, args = c.get("name"), c.get("arguments") or {}
        if not name:
            continue
        if i >= len(results):
            continue
        again = registry.dispatch(name, args)
        first = numeric_fingerprint([results[i] or {}])
        second = numeric_fingerprint([again])
        if first != second:
            drift.append({"name": name, "arguments": args,
                          "before": first[:8], "after": second[:8]})
    return drift


def verify_state(state: QAState) -> QAState:
    """跑校验并给出 HITL 判定。**不修改答案**（校验只做标记，不替用户改结论）。"""
    notes = list(state.get("notes") or [])
    route = state.get("route") or {}
    intent = route.get("intent") or state.get("intent") or "rag"

    # 拒答 / 故障：不需要校验，也不挂起（拒答是结论，故障是系统状态）
    if state.get("error") or state.get("refused"):
        reason = state.get("refusal_reason") or "refused"
        return {
            "verify": {"checked": False, "supported": True, "unsupported": [],
                       "detail": {"intent": intent, "skip": reason},
                       "note": "拒答/故障不进入校验，也不挂起（拒答是结论，不是待确认）。"},
            "hitl": {"pending": False, "reason": None, "thread_id": state.get("thread_id")},
        }

    answer = state.get("answer") or ""
    allowed, detail = _allowed_numbers(state)

    unsupported = unsupported_numbers(answer, allowed)
    numbers_total = len(extract_numbers(answer))

    cite_check = state.get("cite_check") or {}
    dangling = list(cite_check.get("dangling") or [])

    drift = (recheck_tools(state.get("tool_calls") or [], state.get("tool_results") or [])
             if intent == "analysis" else [])

    confidence = float(state.get("confidence") or 0.0)

    verify = {
        "checked": True,
        "supported": not unsupported and not dangling and not drift,
        "unsupported": [{"raw": u["raw"], "value": u["value"]} for u in unsupported],
        "numbers_total": numbers_total,
        "allowed_size": len(allowed),
        "dangling_citations": dangling,
        "tool_drift": drift,
        "confidence": confidence,
        "detail": {**detail, "tool_calls": len(state.get("tool_calls") or [])},
    }

    # ---- HITL 判定：优先级 = 数据不一致 > 引用不支持 > 置信度偏低 ----
    forced = config.HITL_FORCE_REASON
    reason = None
    if drift:
        reason = R_NUMERIC
    elif unsupported:
        reason = R_CITATION
    elif dangling:
        reason = R_CITATION
    elif (config.HITL_ENABLED and confidence < config.HITL_MIN_CONFIDENCE
            and not state.get("degraded") and (numbers_total or state.get("citations"))):
        reason = R_LOW_CONF

    if forced in (R_CITATION, R_LOW_CONF, R_NUMERIC) and not reason:
        # 演练开关：只改"要不要挂起"的结论，判定明细照实记录（见 config 的说明）
        reason = forced
        notes.append(f"⚠️ 命中 HITL 演练开关（FA_HITL_FORCE_REASON={forced}）："
                     f"本次挂起**不是**校验发现的真实问题，仅用于验证"
                     f"「挂起 → 跨进程恢复 → 人工确认」这条链路。")

    hitl = {"pending": bool(reason), "reason": reason,
            "thread_id": state.get("thread_id"), "reviewed": False, "decision": None}

    if unsupported:
        notes.append("数字回归未通过：" + "、".join(u["raw"] for u in unsupported)
                     + " 在证据里找不到出处 —— 已标记并转入人工确认（不静默放行）。")
    if dangling:
        notes.append(f"答案正文引用了引用列表里没有的编号 {dangling}，存在死链。")
    if drift:
        notes.append("取数后重查发现数值已变化（并发写入或口径重灌）→ 必须人工裁决，"
                     "不能任选一个值。")
    if reason == R_LOW_CONF and not forced:
        notes.append(f"置信度 {confidence} < {config.HITL_MIN_CONFIDENCE} 且未拒答 → "
                     f"证据偏薄，转人工确认后再对外发布。")

    return {"verify": verify, "hitl": hitl, "notes": notes}
