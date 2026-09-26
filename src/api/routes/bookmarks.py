"""Phase 2: bookmarks CRUD — 用户自建书签（关键问答 + 引用源快照）。"""
from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src import db
from src import auth as auth_mod

router = APIRouter()


class BookmarkCreate(BaseModel):
    label: str = Field(..., description="书签标签，如「茅台2024资产」")
    thread_id: str | None = None
    question: str | None = None
    answer_excerpt: str | None = None
    citation_refs: str | None = None
    note: str | None = None


@router.get("/api/bookmarks")
def api_list_bookmarks(
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    rows = db.list_bookmarks(user["username"])
    return {"ok": True, "bookmarks": rows, "count": len(rows)}


@router.post("/api/bookmarks")
def api_create_bookmark(
    req: BookmarkCreate,
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    b_id = _uuid.uuid4().hex
    db.add_bookmark(
        b_id, user["username"], req.label,
        thread_id=req.thread_id, question=req.question,
        answer_excerpt=req.answer_excerpt, citation_refs=req.citation_refs,
        note=req.note,
    )
    return {"ok": True, "bookmark_id": b_id}


@router.delete("/api/bookmarks/{bookmark_id}")
def api_delete_bookmark(
    bookmark_id: str,
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    deleted = db.delete_bookmark(bookmark_id, user["username"])
    if not deleted:
        raise HTTPException(status_code=404, detail=f"bookmark_id={bookmark_id} 不存在")
    return {"ok": True, "bookmark_id": bookmark_id}
