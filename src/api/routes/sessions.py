"""Phase 2: sessions CRUD — 会话元数据的后端持久化与查询。

前端 sessionStore 原先只在 localStorage 存会话摘要；批次 1 接这组端点后，
会话能跨设备同步 + 搜索。完整对话 transcript 仍在 LangGraph Checkpointer 里（独立后端），
这里只存摘要（thread_id / title / intent / pending / turns_count / last_at）。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from src import db
from src import auth as auth_mod

router = APIRouter()


class SessionTouchReq(BaseModel):
    """轻量触达：不强制 upsert 全部字段，只刷 last_at / pending / title / intent / turns_delta。"""
    title: str | None = None
    first_question: str | None = None
    intent: str | None = None
    pending: bool | None = None
    turns_delta: int = 0


@router.get("/api/sessions")
def api_list_sessions(
    limit: int = Query(50, ge=1, le=500),
    q: str | None = Query(None, description="标题/首问模糊搜"),
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    rows = db.list_sessions(user["username"], limit=limit, q=q)
    return {"ok": True, "sessions": rows, "count": len(rows), "limit": limit, "q": q}


@router.get("/api/sessions/{thread_id}")
def api_get_session(
    thread_id: str,
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    row = db.get_session(thread_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"thread_id={thread_id} 不存在")
    if row["username"] != user["username"]:
        raise HTTPException(status_code=404, detail=f"thread_id={thread_id} 不存在")
    return {"ok": True, "session": row}


@router.post("/api/sessions/{thread_id}/touch")
def api_touch_session(
    thread_id: str,
    req: SessionTouchReq = Body(...),
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    db.upsert_session(
        thread_id, user["username"],
        title=req.title, first_question=req.first_question,
        intent=req.intent, pending=req.pending,
        turns_delta=req.turns_delta,
    )
    return {"ok": True, "thread_id": thread_id}


@router.delete("/api/sessions/{thread_id}")
def api_delete_session(
    thread_id: str,
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    deleted = db.delete_session(thread_id, user["username"])
    if not deleted:
        raise HTTPException(status_code=404, detail=f"thread_id={thread_id} 不存在")
    return {"ok": True, "thread_id": thread_id}
