"""Phase 2: ingest_batches — 入库批次记录。

批次 1 里这组端点**不改变** /api/ingest/commit 的现有行为，
只在 commit 时追加一条 ingest_batches 记录（批次 1.1 再把 wizard 流程串上）。
所以这组端点现在主要给前端做「我过去的入库历史」查询用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from src import db
from src import auth as auth_mod

router = APIRouter()


@router.get("/api/ingest/batches")
def api_list_batches(
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    rows = db.list_ingest_batches(user["username"], limit=limit)
    return {"ok": True, "batches": rows, "count": len(rows), "limit": limit}
