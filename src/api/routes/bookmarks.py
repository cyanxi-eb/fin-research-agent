"""Phase 2: bookmarks CRUD — 用户自建书签（关键问答 + 引用源快照）。"""
from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src import db
from src import auth as auth_mod
from src import sanitize as sanitize_mod

router = APIRouter()


class BookmarkCreate(BaseModel):
    label: str = Field(..., max_length=200, description="书签标签")
    thread_id: str | None = Field(None, max_length=64)
    question: str | None = Field(None, max_length=2000)
    answer_excerpt: str | None = Field(None, max_length=4000)
    citation_refs: str | None = Field(None, max_length=1000)
    note: str | None = Field(None, max_length=1000)

    @field_validator("label", "question", "answer_excerpt", "note")
    @classmethod
    def _sanitize_text_fields(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return sanitize_mod.sanitize_text(v)

    @field_validator("thread_id")
    @classmethod
    def _sanitize_thread_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return sanitize_mod.sanitize_identifier(v)

    @field_validator("citation_refs")
    @classmethod
    def _sanitize_refs(cls, v: str | None) -> str | None:
        # citation_refs 是 JSON 字符串，可能含特殊字符 —— 只 strip
        if v is None:
            return None
        return v.strip()


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
