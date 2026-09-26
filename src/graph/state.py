"""RAG 问答子图的状态定义。

为什么单独一个文件而不是写在 nodes 里：Step 5 的主图要把这个子图**当节点挂进去**，
主图需要知道"这个子图吃进什么、吐出什么"。状态定义就是这个接口契约 ——
放在这里，主图与子图引同一份，不会出现两边字段名不一致的低级错误。

字段分三组（对应三个节点）：
- 入口参数：`question / code / year / topk / mode / use_llm / intent`
- 检索产出：`hits / retrieval`
- 生成与引用：`answer / refused / refusal_reason / evidence / citations /
  confidence / degraded / llm / notes / disclaimer / error`
"""
from __future__ import annotations

from typing import Any, TypedDict


class QAState(TypedDict, total=False):
    """问答子图状态。`total=False` 让每个节点只写自己产出的字段。"""

    # ---- 入口参数 ----
    question: str
    # 意图标签。Step 3 固定 "rag"（本步只有文档问答一条路）；
    # Step 5 引入 router 后由路由节点填 "rag" / "analysis" / "compliance"。
    # 现在就放进状态，是为了让下游（审计、HITL、前端）不必等到 Step 5 才改字段。
    intent: str
    code: str | None
    year: int | None
    topk: int | None
    mode: str | None
    use_llm: bool

    # ---- 多轮对话（Step 6：指代消解）----
    # 最近若干轮的实体（`{question, intent, code, year}`，旧→新），用于消解
    # "它 / 该公司" 这类指代。**不进 `to_response()`**：它只是图内部消解指代用的
    # 上下文，导出到响应里会让响应体随轮次线性膨胀，而客户端并不需要它。
    history: list[dict]

    # ---- retrieve 节点产出 ----
    hits: list[dict]
    retrieval: dict

    # ---- generate 节点产出 ----
    answer: str
    refused: bool
    refusal_reason: str | None
    evidence: dict
    degraded: bool
    llm: dict | None

    # ---- cite 节点产出 ----
    citations: list[dict]
    confidence: float
    cite_check: dict
    notes: list[str]
    disclaimer: str

    # ---- Step 5：编排与校验（只加不删，见 Step5-设计要点.md §3）----
    # 路由决策 + **为什么这么路由**。`rule` 必须留下：路由错了会伪装成
    # "检索不准/模型不行"，没有 rule 字段几乎无法排查。
    route: dict
    # 工具调用记录：[{name, arguments, ok, summary, source}]
    tool_calls: list[dict]
    # 工具**原始返回**（工具层 dispatch 的完整 payload）。
    # 为什么不塞进 tool_calls：那是给响应/审计看的摘要，塞进完整 payload 会让
    # 响应体膨胀几倍；但 verify 的数字回归需要原始值，所以单独留一槽（仅在图内流转）。
    tool_results: list[dict]
    # 引用校验明细：{checked, supported, unsupported, numbers, detail}
    verify: dict
    # 合规路径命中的法规条文原文（与 `hits` 分开：`hits` 专指年报片段，
    # 混用会让"这段文字来自哪条语料"变得要靠猜 —— 而引用可信度全靠这个）
    regulation_hits: list[dict]
    # HITL：{pending, reason, thread_id, reviewed, decision, detail}
    hitl: dict
    # Checkpointer 的会话键（挂起/恢复都用它）
    thread_id: str
    # 强制指定意图（跳过路由判定）。留给 API 的显式指定与回归测试用：
    # 有它就能"固定走某条链路"复现问题，而不用靠改问题措辞去凑路由。
    force_intent: str | None

    # ---- 兜底：网络搜索（本轮新增）----
    # `web_search` 是**入口参数**：None = 跟随 config.WEB_SEARCH_ENABLED
    # （调用方显式 True/False 可覆盖，用于"这一次先别联网"或离线验收）。
    # `web` 是兜底节点的产出，**必须导出**（前端靠它渲染网络结果卡片）：
    # 两套引用口径（年报=公司+年份+页码+章节，网络=URL+抓取时间）不能混在一条列表里。
    web_search: bool | None
    web: dict

    # ---- 故障（与"拒答"严格区分：故障是系统状态，拒答是结论）----
    error: str | None
    message: str | None


def initial_state(question: str, *, code: str | None = None, year: int | None = None,
                  topk: int | None = None, mode: str | None = None,
                  use_llm: bool = True, thread_id: str | None = None,
                  force_intent: str | None = None,
                  history: list[dict] | None = None,
                  web_search: bool | None = None) -> QAState:
    """构造入口状态（把默认值集中在一处，避免每个调用点各写一遍）。

    `history` 是最近若干轮对话的**实体**（`{question, intent, code, year}`），
    供 `filters.resolve_entities` 消解"它 / 该公司"这类指代；这里顺手做**上限裁剪**，
    使"只保留最近 N 轮"这条纪律只有一个落点（配置见 `config.HISTORY_MAX`）。
    """
    # 延迟导入：state 是图层的接口契约，不想在模块加载期就把检索层拖进来
    from src.retrieve.filters import trim_history

    return {
        "question": (question or "").strip(),
        "intent": force_intent or "rag",
        "code": code,
        "year": year,
        "topk": topk,
        "mode": mode,
        "use_llm": use_llm,
        "thread_id": thread_id,
        "force_intent": force_intent,
        "web_search": web_search,
        "history": trim_history(history),
        "hits": [],
        "citations": [],
        "tool_calls": [],
        "tool_results": [],
        "regulation_hits": [],
        "notes": [],
        "refused": False,
        "degraded": False,
        "confidence": 0.0,
        "hitl": {"pending": False},
        # `web` 初值给 None（不是 {}）：空字典会让前端分不清"这次没跑联网"
        # 与"跑了但一条结果都没有"，而这两种状态要给的提示完全不同。
        "web": None,
        "error": None,
    }


def to_response(state: QAState) -> dict[str, Any]:
    """状态 → 对外响应。**唯一的出口转换**，保证 HTTP 与直接调用返回同形。"""
    web = state.get("web")
    return {
        "ok": state.get("error") is None,
        "error": state.get("error"),
        "message": state.get("message"),
        "question": state.get("question", ""),
        "intent": state.get("intent", "rag"),
        "route": state.get("route"),
        "thread_id": state.get("thread_id"),
        "answer": state.get("answer", ""),
        "citations": state.get("citations", []),
        "refused": state.get("refused", False),
        "refusal_reason": state.get("refusal_reason"),
        "degraded": state.get("degraded", False),
        "confidence": state.get("confidence", 0.0),
        "evidence": state.get("evidence"),
        "retrieval": state.get("retrieval"),
        "tool_calls": state.get("tool_calls") or [],
        "verify": state.get("verify"),
        "hitl": state.get("hitl") or {"pending": False},
        "cite_check": state.get("cite_check"),
        "llm": state.get("llm"),
        "notes": state.get("notes") or [],
        "disclaimer": state.get("disclaimer"),
        # 联网兜底的结果（None = 这次没跑；{} 是不可能的，见 initial_state 的说明）
        "web": web,
        # 网络引用**独立成字段**：它与上面的 `citations`（年报口径：公司+年份+页码+章节）
        # 是两套口径，混在一条列表里会让"这个数字出自哪"再也核不实（决策 D2）。
        "web_citations": (web or {}).get("citations") if web else None,
    }
