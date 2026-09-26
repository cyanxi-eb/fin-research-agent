"""图编译 —— 两套：Step 3 的问答子图，与 Step 5 的完整主图。

```
question ─▶ router ─┬─▶ rag        ─┐
         (规则优先)  ├─▶ analysis   ─┼─▶ verify ─┬─▶ finalize ─▶ response
                     └─▶ compliance ─┘           ├─▶ hitl（interrupt，挂起）
                                                 └─▶ websearch ─▶ finalize
                                                    （仅在拒答且原因是"本地没有"时）
```

## 为什么留两套图而不是把老的改掉

`build_qa_graph()` 是**强制走文档问答**的入口：评测（`scripts/eval_retrieval.py`）、
Step 3/4 的契约测试、以及"我就想看看这几页原文怎么说"的用法都依赖它。
把路由混进去，这些用途就得先绕过路由，反而更绕。
所以：
- `run_qa()`  —— 强制 RAG（不过路由）。语义与 Step 3 完全一致，老调用点零改动；
- `run_agent()` —— 完整编排（路由 + 三子图 + 校验 + HITL + 跨进程恢复）。

两条入口**共用同一批节点实现**（`nodes.py`），不存在"图里一套、直接调一套"的漂移。

## 边界（都在这里定死）

1. **子图只吃 `QAState`、只吐 `QAState`** —— 主图挂它时才不用关心内部；
2. **`build_*` 不缓存编译产物**：LangGraph 的编译产物不可跨进程 pickle，
   缓存在模块级会在多 worker / 热重载时出怪问题。编译很便宜，每次新建；
3. **Checkpointer 只挂在主图**：子图自己接一个没用的 Checkpointer，
   只会让"为什么库里多了张表"变成谜。
"""
from __future__ import annotations

import uuid

from src import audit as audit_mod
from src import config
from src.graph import checkpoint as ckpt
from src.graph.nodes import (analysis_node, compliance_node, finalize_node,
                             hitl_node, retrieve_node, router_node, verify_node)
from src.graph.state import QAState, initial_state, to_response
from src.graph.websearch_node import (should_websearch_after_verify,
                                      websearch_node)
from src.retrieve.filters import trim_history

# 允许的意图（与 router.INTENTS 一致；这里再列一次是为了"条件边的返回值必须落在
# 分支映射表里"这件事有个显式断言点，拼错会立刻报错而不是静默走 default）
_INTENTS = ("rag", "analysis", "compliance")


# ==================== Step 3：RAG 子图（保持原语义）====================

def build_qa_graph():
    """编译问答子图（`retrieve → generate → cite`，未接 Checkpointer）。"""
    from langgraph.graph import END, START, StateGraph

    from src.graph.nodes import cite_node, generate_node

    g = StateGraph(QAState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("generate", generate_node)
    g.add_node("cite", cite_node)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", "cite")
    g.add_edge("cite", END)
    return g.compile()


def run_qa(question: str, *, code: str | None = None, year: int | None = None,
           topk: int | None = None, mode: str | None = None,
           use_llm: bool = True, thread_id: str | None = None,
           history: list[dict] | None = None,
           web_search: bool | None = None) -> dict:
    """跑一遍**文档问答子图**并转成对外响应（`state.to_response` 是唯一出口）。

    不过路由、不接 Checkpointer —— 这是"强制 RAG"的入口，语义与 Step 3 一致。
    `history` 是多轮上下文（最近若干轮实体），用于指代消解。
    `web_search` 只在状态里占位：子图里没有兜底节点（兜底挂在主图 `verify` 之后），
    留着它是为了让两条入口的入参形状一致，避免调用方记两套。
    """
    graph = build_qa_graph()
    state = initial_state(question, code=code, year=year, topk=topk, mode=mode,
                          use_llm=use_llm, thread_id=thread_id, history=history,
                          web_search=web_search)
    # 即使不过路由，也把 `route` 标成"被强制"，这样响应形状与主图一致，
    # 且排查时能看出"这条是被人为指定走 RAG 的"，不会误以为是路由器判的。
    state["route"] = {"intent": "rag", "rule": "forced_rag", "confidence": 1.0,
                      "matched": [], "reason": "调用方显式走问答子图（未经过路由）"}
    final = graph.invoke(state)
    return to_response(final)


# ==================== Step 5：完整主图 ====================

def rag_node(state: QAState) -> QAState:
    """把 RAG 子图当节点跑（`retrieve → generate → cite`）。

    子图返回的是**完整状态**，这里整体透传：只挑几个键返回，就得维护一张
    "哪些键该透传"的清单，一旦子图新增字段就会静默丢失 —— 而"字段悄悄没了"
    比"多传几个字段"难查得多。
    """
    return build_qa_graph().invoke(state)


def _by_intent(state: QAState) -> str:
    intent = ((state.get("route") or {}).get("intent") or "rag")
    return intent if intent in _INTENTS else "rag"


def _after_verify(state: QAState) -> str:
    """`verify` 之后的三路条件边。**顺序即优先级**，不能调换：

    1. `hitl.pending` → `hitl`：**人工确认优先**。挂起中的流程直接去联网，
       等于让人对着一个"还没确认"的结论等网络请求，而且恢复后结论会变两次；
    2. 拒答且原因是"本地资料里没有"（`no_evidence / out_of_corpus / low_coverage`）
       且开关打开 → `websearch`；
    3. 其余 → `finalize`。

    ⚠️ `_INTENTS` 与 `router.INTENTS` **一律不动**：联网是兜底节点，不是意图
    （见 `websearch_node` 的模块说明）。

    "该不该联网"这一条**不在这里判断**，而是调
    `websearch_node.should_websearch_after_verify` —— 流式路径（手工跑节点）
    也要做同一个判断，两处各写一遍必然漂移。
    """
    if (state.get("hitl") or {}).get("pending"):
        return "hitl"
    return "websearch" if should_websearch_after_verify(state) else "finalize"


def build_agent_graph(*, with_checkpointer: bool = True):
    """编译完整主图。`with_checkpointer=False` 用于不需要挂起的纯结构测试。"""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(QAState)
    g.add_node("router", router_node)
    g.add_node("rag", rag_node)
    g.add_node("analysis", analysis_node)
    g.add_node("compliance", compliance_node)
    g.add_node("verify", verify_node)
    g.add_node("hitl", hitl_node)
    g.add_node("websearch", websearch_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", _by_intent,
                            {name: name for name in _INTENTS})
    for name in _INTENTS:
        g.add_edge(name, "verify")
    g.add_conditional_edges("verify", _after_verify,
                            {"hitl": "hitl", "websearch": "websearch",
                             "finalize": "finalize"})
    g.add_edge("hitl", "finalize")
    # 兜底节点跑完也要定稿：免责声明、dangling 备注、审计都在 finalize 里收口，
    # 绕过它会让"联网兜底过"的这一次响应与别的响应不同形。
    g.add_edge("websearch", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=ckpt.make_checkpointer() if with_checkpointer else None)


def new_thread_id() -> str:
    return f"fa-{uuid.uuid4().hex[:12]}"


def run_agent(question: str, *, code: str | None = None, year: int | None = None,
              topk: int | None = None, mode: str | None = None,
              use_llm: bool = True, thread_id: str | None = None,
              force_intent: str | None = None, audit: bool = True,
              actor: str | None = None,
              history: list[dict] | None = None,
              web_search: bool | None = None) -> dict:
    """完整编排入口（路由 + 三子图 + 校验 + HITL）。

    返回**与 `run_qa` 同形**（同为 `to_response`），因此 HTTP 面与脚本面不会漂移。

    ⚠️ "被挂起"不是错误：响应里的 `hitl.pending == True` 就是等待人工确认的信号，
    `ok` 仍为 true。客户端拿这个 `thread_id` 去确认即可（见 `resume_agent`）。

    审计默认开：一个"能回答但答得对不对"的系统，没有留痕就没法复盘。
    `audit=False` 只留给单测（避免污染审计表、也避免用例受表结构影响）。

    `history` 是多轮上下文（最近若干轮实体），用于指代消解；`to_response` 不导出它。
    `web_search` 为 None 时跟随 `config.WEB_SEARCH_ENABLED`（见 `websearch_node`）。
    """
    import time

    from src import audit as audit_mod

    tid = thread_id or new_thread_id()
    graph = build_agent_graph()
    state = initial_state(question, code=code, year=year, topk=topk, mode=mode,
                          use_llm=use_llm, thread_id=tid, force_intent=force_intent,
                          history=history, web_search=web_search)
    t0 = time.monotonic()
    out = graph.invoke(state, {"configurable": {"thread_id": tid}})
    resp = to_response(out)
    if audit:
        audit_mod.record_ask(resp, thread_id=tid, actor=actor,
                             latency_ms=int((time.monotonic() - t0) * 1000))
    return resp


def resume_agent(thread_id: str, decision: dict | str, *, audit: bool = True,
                 actor: str | None = None,
                 history: list[dict] | None = None,
                 web_search: bool | None = None) -> dict:
    """用同一 `thread_id` 恢复被挂起的流程（可跨进程 —— 状态在 Checkpointer 里）。

    `decision` 形如 `{"decision": "approve", "note": "...", "reviewer": "..."}`。

    `history` 可选：给了就把会话上下文一并写回该 thread 的状态（只更新 `history`
    这一个槽，不动其余字段）。不传则沿用挂起那一刻已存的状态，行为与 Step 5 完全一致。
    """
    from langgraph.types import Command

    from src import audit as audit_mod

    graph = build_agent_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(cfg)
    if not snapshot.next:
        raise ValueError(f"thread_id={thread_id!r} 没有处于挂起状态的流程，无法恢复")
    if history is not None:
        graph.update_state(cfg, {"history": trim_history(history)})
    if web_search is not None:
        # 恢复时也能改联网开关（默认沿用挂起那一刻存下来的值）
        graph.update_state(cfg, {"web_search": bool(web_search)})
    out = graph.invoke(Command(resume=decision), cfg)
    resp = to_response(out)
    if audit:
        audit_mod.log("hitl_confirm", target=thread_id, actor=actor,
                      detail={"decision": decision,
                              "reason": (resp.get("hitl") or {}).get("reason"),
                              "question": resp.get("question")})
    return resp


def agent_status(thread_id: str) -> dict:
    """读回某会话的当前状态（挂起中 / 已完成），**不改动它**。

    这是"重启服务后仍能读回挂起流程"这条验收标准的读取口：
    用同一个 `thread_id` 从 Checkpointer 里取回的就是挂起那一刻的完整状态。
    """
    graph = build_agent_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    snap = graph.get_state(cfg)
    if not snap or not snap.values:
        return {"thread_id": thread_id, "found": False, "pending": False,
                "next": [], "payload": None}
    payload = None
    for task in (snap.tasks or ()):
        for itr in (getattr(task, "interrupts", ()) or ()):
            payload = getattr(itr, "value", None)
            break
        if payload:
            break
    return {
        "thread_id": thread_id,
        "found": True,
        "pending": bool(snap.next),
        "next": list(snap.next or []),
        "payload": payload,
        "response": to_response(snap.values),
    }


def load_history(thread_id: str | None, *, limit: int | None = None) -> list[dict]:
    """从 Checkpointer 读回某会话的历史轮次**实体**（只取 `{question, intent, code, year}`，不取答案）。

    这是多轮问答的读取口：上下文同样落在 Checkpointer 里，所以换进程 / 换 worker
    都能续上（"上一轮问的茅台，这一轮问'它'"）。

    实体以**状态里已定稿的值**为准：`code/year` 优先取 `retrieval.filters` 里
    实际用过的过滤条件（它才是"这一轮到底查了哪家/哪年"，含从更早轮次沿用来的），
    而不是只看问句文本里显式写了什么 —— 否则多轮的沿用链会一轮就断。
    """
    if not thread_id:
        return []
    graph = build_agent_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    snap = graph.get_state(cfg)
    if not snap or not snap.values:
        return []
    vals = snap.values
    used = (vals.get("retrieval") or {}).get("filters") or {}
    round_ = {
        "question": vals.get("question"),
        "intent": vals.get("intent"),
        "code": vals.get("code") or used.get("code"),
        "year": vals.get("year") if vals.get("year") is not None else used.get("year"),
    }
    hist = [h for h in (vals.get("history") or []) if isinstance(h, dict)]
    hist.append(round_)
    return trim_history(hist, limit)


def persist_pending(state: QAState, thread_id: str | None = None) -> str | None:
    """把**待人工确认**的状态写进 Checkpointer，并真正落下一个**可恢复的挂起点**。

    与 `persist_session` 的差别是"存哪一个状态、存成什么形态"：

    - `persist_session` 存**已定稿**态（`as_node="finalize"`，`next` 为空）；
    - 本函数存 **`verify` 之后、`finalize` 之前**的态（`as_node="verify"`）。
      `verify` 的出口是条件边 `_after_verify`，它按 `hitl.pending` 选出 `hitl`，
      所以写完 `next` 就是 `("hitl",)`；随后 `invoke(None, ...)` 真正执行
      `hitl_node`，由 `interrupt()` 把挂起点落盘。

    为什么非这么写不可：流式路径是**手工跑节点**的（不走 `graph.invoke`）。若挂起时
    仍按"已完成"落库，库里 `next` 为空 —— 人点「确认放行」会命中 `resume_agent` 里的
    `if not snapshot.next: raise ValueError` → HTTP **409**，现象是"前端弹了确认面板，
    后端却说没有挂起的流程"。

    传 `verify` 态而不是 `finalize` 态，还有一个必须避开的副作用：`finalize_node` 会
    补免责声明、追加 dangling 备注、给 reject 加「不予发布」前缀，**都不是幂等的**。
    若把定稿态当挂起态存下，恢复后 `finalize` 再跑一次就会重复追加一遍。

    异常同样**吞掉**（与 `persist_session` 同口径）：挂起点写失败不该让一次已算好的
    答案 500，最坏是这次确认拿到 409。
    """
    tid = thread_id or (state or {}).get("thread_id")
    if not tid:
        return None
    try:
        graph = build_agent_graph()
        cfg = {"configurable": {"thread_id": tid}}
        graph.update_state(cfg, dict(state or {}), as_node="verify")
        graph.invoke(None, cfg)
    except Exception as e:         # noqa: BLE001 —— 见 docstring：不影响已算出的答案
        # 但**必须留痕**：写挂起点失败时前端照样会弹确认面板，人点下去必然 409 ——
        # 症状与"挂起态被按已完成落库"这个已修缺陷**一模一样**。不留一条审计，
        # 下次遇到就只剩"面板弹了但后端说没挂起"这一句话可查（无法区分两种成因）。
        audit_mod.log("hitl_persist_failed", target=tid,
                      detail={"thread_id": tid, "error": f"{type(e).__name__}: {e}"})
        return None
    return tid


def persist_session(state: QAState, thread_id: str | None = None) -> str | None:
    """把**已定稿**的状态写进 Checkpointer（不重跑整图 → 不重复调模型）。

    流式路径（`/api/ask/stream`）是手工跑节点的，不走 `graph.invoke`，因此若不显式
    落库，这个 `thread_id` 在库里就是空的 —— 后果是功能级的：
      - `load_history()` 读不回上一轮 → **纯 SSE 会话的多轮指代消解失效**；
      - `GET /api/citations` 依赖 `agent_status()` 读库 → 引用卡片会 404。

    写入口用 `update_state(..., as_node="finalize")`：`finalize` 在图上直连 `END`，
    所以对**全新 thread** 也能一步建出"已完成"的 checkpoint（`next` 为空），
    无需把整张图再跑一遍（那会重复调用模型、重复计费）。

    这里**吞掉写入异常**：持久化失败不该让一次已经算好的答案 500 ——
    最坏是这一轮读不回上下文，而不是用户拿不到答案。
    """
    tid = thread_id or (state or {}).get("thread_id")
    if not tid:
        return None
    try:
        build_agent_graph().update_state(
            {"configurable": {"thread_id": tid}}, dict(state or {}), as_node="finalize")
    except Exception:              # noqa: BLE001 —— 见 docstring：不影响已算出的答案
        return None
    return tid


if __name__ == "__main__":
    # 自检：python -m src.graph.builder "五粮液和贵州茅台2024年的毛利率对比" [--rag]
    import json as _json
    import sys as _sys

    _args = [a for a in _sys.argv[1:] if not a.startswith("--")]
    _q = " ".join(_args) or "五粮液和贵州茅台2024年的毛利率对比"
    _runner = run_qa if "--rag" in _sys.argv else run_agent
    print(_json.dumps(_runner(_q, use_llm=False), ensure_ascii=False, indent=2))
