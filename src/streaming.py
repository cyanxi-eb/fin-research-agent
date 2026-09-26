"""SSE 流式问答 —— 把「路由 → 检索 → 生成」拆成可逐步下发的事件序列。

## 一条铁律：**流式与一次性两条路的输出必须同形**

前端只写一套渲染。若流式的 `done` 事件与 `/api/ask` 的响应结构不同，
前端就得写两套渲染分支 —— 那是**必然漂移**的双份逻辑（改了一处忘了另一处）。
所以本模块的做法是：

- 终态**一律走 `answer.synthesize(...)`** 产出，`src/graph/state.py::to_response()`
  是唯一出口；
- 流式只负责"把模型原文一段段发出去"和"把事件按协议排队"，
  既不自己另拼响应，也不自己写引用校验（复用 `answer.validate_citations` /
  `answer.collect_citation_list`）。
- 流式拿到模型原文后，通过 `synthesize(..., raw_model_output=...)` 收口，
  与一次性路径走的是**同一条下游**（解析 JSON → 校验编号 → 降级）。

## 事件协议（`EVENTS`）

| 事件 | 何时发 | 载荷 |
|---|---|---|
| `meta`      | 第一个，**必发** | `intent` / `route` / `thread_id`（+ `question`） |
| `token`     | 逐段 | `{"text": ...}`；不流式时只发一次（整段答案） |
| `citations` | 终态前 | `{"citations": [...], "count": n}` |
| `verify`    | 终态前 | 与 `to_response()["verify"]` 同物 |
| `web`       | 仅联网兜底真跑过时（`verify` 后、`done` 前） | 与 `to_response()["web"]` 同物 |
| `hitl`      | 仅挂起时（`verify` 后、`done` 前） | 与 `to_response()["hitl"]` 同物 |
| `done`      | 最后一个 | `{"response": <to_response 同形>}` |
| `error`     | 预留 | 目前一律走降级路径，不单独发 `error`（**绝不静默中断**） |

`intent = analysis / compliance` 两条链路**不流式**：它们的答案本来就是确定性拼装
（工具层 / 法规库），没有 token 可流，`meta` 后直接给 `citations → verify → done`。

## 环境故障与内容降级的界线

- **索引缺失**（检索后端未就绪）在**首个事件之前**抛出 `FileNotFoundError` ——
  这样 HTTP 层还能给 503，而不是"发了一半事件再断"。它是**系统状态**，不是答案。
- **模型超时/异常/未配置 Key** 一律回落为「一次性 `token`（降级摘录）+ `done`」，
  `done.response.degraded=True` 且 `notes` 写明原因 —— 让人看得见"这次没走模型"。
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

from src import answer, audit as audit_mod, config, llm
from src.graph import builder
from src.graph import router as router_mod
from src.graph import state as state_mod
from src.graph.nodes import analysis_node, compliance_node, finalize_node, verify_node
from src.graph.websearch_node import (should_websearch_after_verify,
                                      websearch_node)
from src.retrieve import pipeline

# 事件名集合。`error` 先在协议里占位：一旦将来需要显式的错误事件，前端不必改解析器。
EVENTS: tuple[str, ...] = ("meta", "token", "citations", "verify", "web", "hitl",
                           "done", "error")

# 终态响应里与 `to_response()` 同名的字段：从打包结果整体搬到状态，避免逐字段手抄。
_PACKED_KEYS = ("answer", "refused", "refusal_reason", "degraded", "evidence",
                "citations", "confidence", "llm", "notes", "disclaimer", "retrieval")


def _resolve_route(question: str, force_intent: str | None) -> dict:
    """路由（与 `router_node` 同口径，含 `forced_by_request`）。"""
    if force_intent in router_mod.INTENTS:
        return {"intent": force_intent, "rule": "forced_by_request", "confidence": 1.0,
                "matched": [], "reason": f"调用方显式指定走 {force_intent}（未经路由判定）"}
    return router_mod.route(question)


# ==================== 非流式链路（analysis / compliance）====================

def _run_subgraph(question: str, route: dict, intent: str, *, code: str | None,
                  year: int | None, topk: int | None, mode: str | None,
                  thread_id: str, use_llm: bool = True,
                  history: list[dict] | None = None,
                  web_search: bool | None = None) -> state_mod.QAState:
    """跑一条确定性链路（子图 → verify → finalize），**复用既有节点**不自拼响应。

    数值题走工具层、合规题走法规库，两者都是确定性拼装，本就没有 token 可流。
    """
    state = state_mod.initial_state(question, code=code, year=year, topk=topk,
                                    mode=mode, use_llm=use_llm, thread_id=thread_id,
                                    force_intent=intent, history=history,
                                    web_search=web_search)
    state["route"] = route
    state["intent"] = intent
    node = analysis_node if intent == "analysis" else compliance_node
    state.update(node(state) or {})
    state.update(verify_node(state) or {})
    # 与主图 `_after_verify` **同一判据**（含"挂起优先"）：这两条链路极少落到兜底上
    # （拒答原因多为 model_insufficient），但判据一致才不会出现"图里跑了、流式没跑"。
    if should_websearch_after_verify(state):
        state.update(websearch_node(state) or {})
    pending_state = dict(state)     # 挂起时用它落库（须在 finalize 之前；见 _finalize）
    state.update(finalize_node(state) or {})
    return state, pending_state


# ==================== RAG 链路的准备（可在首个事件前抛环境故障）====================

async def _prepare_rag(question: str, *, code: str | None, year: int | None,
                       topk: int | None, mode: str | None,
                       history: list[dict] | None = None) -> tuple[dict | None, list[dict], dict]:
    """RAG 的准备：检索 + 一次 `use_llm=False` 的合成（**不调模型**，只跑闸门）。

    返回 `(res, hits, packed)`：`packed` 是闸门判定结果 ——
    若它 `refused`，说明该拒答（闸门命中，与一次性路径完全同口径）；
    否则它是一份降级摘录，用来在"模型不可用/流式失败"时兜底。

    `history` 是多轮上下文，透传给检索层做指代消解（见 `filters.resolve_entities`）。

    索引缺失时**抛 FileNotFoundError**：让 HTTP 层在流开始前给 503。
    """
    if not question:
        # 空问题：不检索、不调模型（与 `answer_question` 同口径）
        return None, [], answer.answer_question(question)

    res = await asyncio.to_thread(
        pipeline.retrieve, question, topk=topk or config.ANSWER_CANDIDATE_TOPK,
        code=code, year=year, mode=mode, history=history)
    if not res.get("ok") and res.get("error") == "index_missing":
        raise FileNotFoundError(res.get("message") or "检索索引不可用")
    hits = res.get("hits") or []
    packed = answer.synthesize(question, res, use_llm=False)
    return res, hits, packed


def _degraded_from_excerpt(packed: dict, exc: Exception) -> dict:
    """流式失败 → 用已备好的降级摘录收尾，并把原因写进 notes（不静默中断）。"""
    out = dict(packed)
    reason = (f"流式调用大模型失败（{type(exc).__name__}: {exc}），"
              f"已降级为原文摘录（内容有出处，可核对）。")
    notes = list(out.get("notes") or [])
    if notes:
        notes[0] = reason
    else:
        notes = [reason]
    out["notes"] = notes
    out["degraded"] = True
    return out


def _rag_state(question: str, route: dict, thread_id: str, packed: dict,
               hits: list[dict], *, history: list[dict] | None = None,
               web_search: bool | None = None) -> state_mod.QAState:
    """把打包结果组装成状态并跑 verify/finalize —— 终态与一次性路径同形。

    `history` 要一并写进状态：它随后会被 `persist_session` 落库，下一轮
    `load_history` 才能读回**完整**的会话链（否则 SSE 多轮只会剩最近一轮）。

    联网兜底插在 `verify` 与 `finalize` 之间（与主图 `_after_verify` 同一位置、同一判据，
    含"挂起优先"）：`resp["web"] is None` 就是"这次没跑"，与"跑了没结果"分得开。
    """
    state = state_mod.initial_state(question, thread_id=thread_id,
                                    force_intent=route.get("intent"), history=history,
                                    web_search=web_search)
    state["route"] = route
    state["intent"] = route.get("intent") or "rag"
    state["hits"] = hits
    for k in _PACKED_KEYS:
        if k in packed:
            state[k] = packed[k]
    state.update(verify_node(state) or {})
    if should_websearch_after_verify(state):
        state.update(websearch_node(state) or {})
    pending_state = dict(state)     # 挂起时用它落库（须在 finalize 之前；见 _finalize）
    state.update(finalize_node(state) or {})
    return state, pending_state


def _finalize(state: state_mod.QAState, thread_id: str, started_at: float,
              actor: str | None, pending_state: dict | None = None) -> dict:
    """定稿收口：终态响应 → 持久化会话 → 审计留痕。

    三件事捆在一处，是为了让 RAG 与非 RAG 两条链路**共用同一收口** ——
    否则"持久化 / 审计"漏掉一条分支，就会表现为"SSE 会话读不回上一轮"
    或"流式问答在审计表里查无此人"，而这类缺失平时完全看不出来。

    持久化分两种形态（`update_state`，都不重跑整图 → 不重复调模型）：
    - **挂起**（`hitl.pending`）→ `persist_pending`：落下真正可恢复的挂起点，
      否则前端的面板点「确认放行」只会拿到 409；
    - 其余 → `persist_session`：已完成态。
    `pending_state` 是 `verify` 之后、`finalize` 之前的状态快照，只有挂起时才用得上
    （原因见 `builder.persist_pending` 的 docstring）。

    审计走 `record_ask`，与 `/api/ask`（`run_agent`）**同一口径**。
    """
    resp = state_mod.to_response(state)
    if (state.get("hitl") or {}).get("pending"):
        builder.persist_pending(pending_state if pending_state is not None else state,
                                thread_id)
    else:
        builder.persist_session(state, thread_id)
    audit_mod.record_ask(resp, thread_id=thread_id, actor=actor,
                         latency_ms=int((time.monotonic() - started_at) * 1000))
    return resp


async def _rag_events(question: str, route: dict, thread_id: str,
                      prepared: tuple[dict | None, list[dict], dict],
                      *, use_llm: bool, started_at: float, actor: str | None,
                      history: list[dict] | None = None,
                      web_search: bool | None = None) -> AsyncIterator[tuple[str, dict]]:
    """RAG：先定"要不要流式"，再发 token 与终态事件。"""
    res, hits, pre = prepared
    ready = llm.is_ready()[0]      # ⚠️ is_ready() 返回 (bool, reason) 元组

    packed = pre
    streamed = False
    if not pre.get("refused") and use_llm and ready is not False:
        try:
            buf: list[str] = []
            async for chunk in answer._astream_llm(question, hits):   # noqa: SLF001
                if not chunk:
                    continue
                buf.append(chunk)
                yield ("token", {"text": chunk})
            # 用流式已发出的原文收口 —— 不再调一次模型（省 token，且与已发 token 一致）
            packed = answer.synthesize(question, res, use_llm=True,
                                       raw_model_output="".join(buf))
            streamed = True
        except Exception as e:                                   # noqa: BLE001
            packed = _degraded_from_excerpt(pre, e)

    if not streamed:
        # 模型不可用 / 请求要求不调模型 / 流式失败 → 一次性把整段答案作为单个 token 发出
        yield ("token", {"text": packed.get("answer") or "", "final": True})

    state, pending_state = _rag_state(question, route, thread_id, packed, hits,
                                      history=history, web_search=web_search)
    resp = _finalize(state, thread_id, started_at, actor, pending_state)
    yield ("citations", {"citations": resp["citations"], "count": len(resp["citations"])})
    yield ("verify", resp.get("verify") or {})
    # 兜底节点**真跑过**才发 `web`（`None` = 没跑）：关闭联网时事件序列与从前逐字相同
    if resp.get("web") is not None:
        yield ("web", resp["web"])
    if (resp.get("hitl") or {}).get("pending"):
        yield ("hitl", resp["hitl"])
    yield ("done", {"response": resp})


# ==================== 主入口 ====================

async def stream_answer(question: str, *, history: list[dict] | None = None,
                        code: str | None = None, year: int | None = None,
                        mode: str | None = None, topk: int | None = None,
                        use_llm: bool = True, force_intent: str | None = None,
                        thread_id: str | None = None,
                        actor: str | None = None,
                        web_search: bool | None = None) -> AsyncIterator[tuple[str, dict]]:
    """流式问答。产出 `(事件名, 载荷)` 序列，由上层（`src/server.py`）序列化成 SSE。

    每个事件的载荷都是**纯 dict**（可直接 `json.dumps`），这样传输层不用懂业务。

    `history` 是多轮上下文（最近若干轮实体），透传到检索层做指代消解。
    `web_search` 为 None 时跟随 `config.WEB_SEARCH_ENABLED`（见 `websearch_node`）。

    定稿时会**持久化进 Checkpointer**（`builder.persist_session`）并**记录审计**
    （与 `/api/ask` 同一口径），因此纯 SSE 会话也能累积多轮上下文、
    也能被 `GET /api/citations` 读回。
    """
    q = (question or "").strip()
    route = _resolve_route(q, force_intent)
    intent = route["intent"]
    tid = thread_id or builder.new_thread_id()
    started_at = time.monotonic()

    # ---- 准备阶段在**首个事件之前**完成：环境故障才能变成 HTTP 状态码 ----
    # （索引缺失会在这里抛 FileNotFoundError，由 server 层转成 503，而不是"发一半再断"）
    if intent == "rag":
        prepared = await _prepare_rag(q, code=code, year=year, topk=topk, mode=mode,
                                      history=history)

    yield ("meta", {"intent": intent, "route": route, "thread_id": tid, "question": q})

    if intent == "rag":
        async for event in _rag_events(q, route, tid, prepared, use_llm=use_llm,
                                       started_at=started_at, actor=actor,
                                       history=history, web_search=web_search):
            yield event
        return

    # 数值 / 合规：不流式，meta 后直接给 citations → verify（→ web / hitl）→ done
    state, pending_state = await asyncio.to_thread(
        _run_subgraph, q, route, intent, code=code, year=year, topk=topk,
        mode=mode, thread_id=tid, use_llm=use_llm, history=history,
        web_search=web_search)
    resp = _finalize(state, tid, started_at, actor, pending_state)
    yield ("citations", {"citations": resp["citations"], "count": len(resp["citations"])})
    yield ("verify", resp.get("verify") or {})
    if resp.get("web") is not None:
        yield ("web", resp["web"])
    if (resp.get("hitl") or {}).get("pending"):
        yield ("hitl", resp["hitl"])
    yield ("done", {"response": resp})


def serialize_event(name: str, payload: dict) -> str:
    """把一个事件序列化成 SSE 帧（`event:` 行 + `data:` 行 + 空行）。

    放在这里而不是 server 里，是为了让"协议长什么样"只有一处定义；
    `ensure_ascii=False` 保证中文不被转成 `\\uXXXX`（前端读起来是可读文本）。
    """
    import json

    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"