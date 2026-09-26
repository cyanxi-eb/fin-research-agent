"""问答 + HITL + 引用端点（Phase 0-2 逐路由组迁移第六批，最复杂的一组）。

**纯 verbatim copy**：从 server.py 复制过来，不改任何逻辑。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src import auth as auth_mod
from src import config
from src import streaming as streaming_mod
from src.graph import builder
from src.graph import router as router_mod

router = APIRouter()


# --- Pydantic 模型（verbatim from server.py） ---

class AskRequest(BaseModel):
    """提问入参。字段全部对应 `QAState` 的入口参数，不额外发明概念。"""

    question: str = Field(..., min_length=1, description="自然语言问题")
    code: str | None = Field(None, description="限定股票代码，如 600519；留空则从问题里自动识别")
    year: int | None = Field(None, description="限定年份，如 2024；留空则从问题里自动识别")
    topk: int | None = Field(None, ge=1, le=50, description="召回片段数，默认 config.ANSWER_CANDIDATE_TOPK")
    mode: str | None = Field(None, description=f"检索模式，可选 {sorted(config.RETRIEVE_MODES)}")
    use_llm: bool = Field(True, description="False 时跳过模型、直接返回有出处的原文摘录（离线演示用）")
    intent: str | None = Field(
        None, description=f"强制指定意图（跳过路由），可选 {sorted(router_mod.INTENTS)}；"
                          "留空则按规则路由")
    thread_id: str | None = Field(
        None, description="会话键。**传入同一个值可用于后续读取/确认**；留空则自动生成")
    actor: str | None = Field(
        None, description="**备注性质的调用方标识**（审计用）。"
                          "开启鉴权后身份取自令牌（sub），此字段不再作为身份依据")
    web_search: bool | None = Field(
        None, description="联网兜底开关。留空跟随 FA_WEB_SEARCH_ENABLED；"
                          "False 表示这一次不联网（库外问题仍按本地结论拒答）")


class DecisionRequest(BaseModel):
    """人工确认入参。`decision` 是**受控枚举**：审批结论必须可机读，不能是自由文本。"""

    decision: str = Field(..., description="approve（可发布）/ reject（不予发布）")
    note: str | None = Field(None, description="备注，会写进审计与响应 notes")
    reviewer: str | None = Field(None, description="确认人标识")


# --- 辅助函数（verbatim from server.py） ---

def _check_mode(mode: str | None) -> None:
    if mode is not None and mode.lower() not in config.RETRIEVE_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"未知检索模式「{mode}」，可选 {sorted(config.RETRIEVE_MODES)}")


def _check_intent(intent: str | None) -> None:
    if intent is not None and intent not in router_mod.INTENTS:
        raise HTTPException(
            status_code=400,
            detail=f"未知意图「{intent}」，可选 {sorted(router_mod.INTENTS)}")


# --- 端点（verbatim from server.py，@app → @router） ---

@router.post("/api/ask")
def api_ask(req: AskRequest, user: dict = Depends(auth_mod.current_user)) -> dict:
    """问答主入口。"""
    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=400, detail="question 不能为空")
    _check_mode(req.mode)
    _check_intent(req.intent)

    actor = user["sub"] if config.AUTH_ENABLED else (req.actor or user["sub"])

    try:
        return builder.run_agent(
            q, code=req.code, year=req.year, topk=req.topk, mode=req.mode,
            use_llm=req.use_llm, thread_id=req.thread_id,
            force_intent=req.intent, actor=actor,
            web_search=req.web_search,
            history=builder.load_history(req.thread_id))
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=f"检索索引不可用：{e}（先跑 scripts/ingest_all.py）")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.post("/api/ask/stream")
async def api_ask_stream(req: AskRequest,
                         user: dict = Depends(auth_mod.current_user)) -> StreamingResponse:
    """同 `/api/ask`，但以 **SSE** 逐步下发。"""
    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=400, detail="question 不能为空")
    _check_mode(req.mode)
    _check_intent(req.intent)

    gen = streaming_mod.stream_answer(
        q, code=req.code, year=req.year, topk=req.topk, mode=req.mode,
        use_llm=req.use_llm, force_intent=req.intent, thread_id=req.thread_id,
        web_search=req.web_search,
        history=builder.load_history(req.thread_id))

    try:
        first = await gen.__anext__()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503,
                            detail=f"检索索引不可用：{e}（先跑 scripts/ingest_all.py）")
    except StopAsyncIteration:
        raise HTTPException(status_code=500, detail="流式问答未产生任何事件")

    async def body():
        yield streaming_mod.serialize_event(*first)
        async for name, payload in gen:
            yield streaming_mod.serialize_event(name, payload)

    return StreamingResponse(
        body(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/hitl/{thread_id}")
def api_hitl_status(thread_id: str,
                    user: dict = Depends(auth_mod.current_user)) -> dict:
    """读回某会话状态（挂起中 / 已确认），**不改动它**。"""
    st = builder.agent_status(thread_id)
    if not st.get("found"):
        raise HTTPException(status_code=404, detail=f"没有 thread_id={thread_id} 的会话记录")
    return st


@router.post("/api/hitl/{thread_id}/confirm")
def api_hitl_confirm(thread_id: str, req: DecisionRequest,
                     user: dict = Depends(auth_mod.current_user)) -> dict:
    """人工确认并恢复流程。"""
    if req.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400,
                            detail=f"decision 只能是 approve / reject，收到「{req.decision}」")
    decision = {"decision": req.decision, "note": req.note, "reviewer": req.reviewer}
    try:
        return builder.resume_agent(thread_id, decision, actor=req.reviewer)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.get("/api/citations")
def api_citations(thread_id: str | None = Query(None, description="会话键；给出则返回该会话的引用列表"),
                  user: dict = Depends(auth_mod.current_user)) -> dict:
    """引用口径说明；带 `thread_id` 时返回该会话**已定稿**的引用列表。"""
    spec = {
        "report_citation": {
            "example": "贵州茅台2024年年报 P87 第三节 管理层讨论与分析 第2/3段",
            "fields": ["company", "year", "page_no", "section", "part/parts_total", "chunk_id"],
            "note": "页码来自 PDF 解析，章节由节号锚点/页首标题识别（见 Step 1）。",
        },
        "regulation_citation": {
            "example": "《上市公司信息披露管理办法》（中国证监会令第226号）第二十条",
            "fields": ["title", "doc_no", "article_no", "article_label", "chapter",
                       "status", "effective_from", "source_name", "url"],
            "note": "法规为**条文级**引用，且带 `status`（current / superseded）——"
                    "用哪一版必须显式，不能含糊。",
        },
        "numeric_source": {
            "example": "income.TOTAL_OPERATE_INCOME（表.字段）",
            "note": "数值题的每个数都带 `表.字段` 与单位，可回溯到库中那一行。",
        },
        "web_citation": {
            "example": "公司公告｜巨潮资讯网（抓取于 2026-09-23 15:04）",
            "fields": ["title", "url", "source_name", "fetched_at"],
            "note": "网络来源为**另一套引用口径**：给的是 URL 与抓取时间，不给页码与章节，"
                    "两者不可混排（响应里分开放在 `web_citations` 而不是 `citations`）。"
                    "可信度低于年报原文 —— 年报是可核验的一手件，网络来源只是线索。",
        },
    }
    if not thread_id:
        return {"ok": True, "spec": spec}
    st = builder.agent_status(thread_id)
    if not st.get("found"):
        raise HTTPException(status_code=404, detail=f"没有 thread_id={thread_id} 的会话记录")
    resp = st.get("response") or {}
    return {"ok": True, "thread_id": thread_id, "pending": st.get("pending"),
            "intent": resp.get("intent"), "citations": resp.get("citations") or [],
            "web_citations": resp.get("web_citations") or []}
