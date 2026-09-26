"""审计端点（Phase 0-2 逐路由组迁移的第一个试点）。

**纯 verbatim copy**：从 server.py 复制过来，不改任何逻辑、不加任何功能。
APIRouter + @router.get 的唯一作用是把端点从 server.py 挪出去，行为零变更。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from src import audit as audit_mod
from src import auth as auth_mod

router = APIRouter()


@router.get("/api/audit")
def api_audit(limit: int = Query(50, ge=1, le=500),
              user: dict = Depends(auth_mod.current_user)) -> dict:
    """最近审计记录（谁问了什么、走了哪条链路、有没有挂起、人工怎么定的）。"""
    return {"ok": True, "stats": audit_mod.stats(), "rows": audit_mod.recent(limit)}
