"""数据入库向导端点（Phase 0-2 逐路由组迁移第四批）。

**纯 verbatim copy**：从 server.py 复制过来，不改任何逻辑。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from src import auth as auth_mod
from src.ingest import wizard as wizard_mod
from src import sanitize as sanitize_mod

router = APIRouter()


# --- Pydantic 模型（verbatim from server.py + Phase 3 消毒） ---

class IngestPlanRequest(BaseModel):
    """向导第一步入参：一段自然语言需求。"""
    text: str = Field(..., min_length=1, max_length=2000, description="自然语言入库需求")

    @field_validator("text")
    @classmethod
    def _sanitize_text(cls, v: str) -> str:
        return sanitize_mod.sanitize_text(v)


class IngestCommitRequest(BaseModel):
    """向导第五步入参：需求单 + 勾选格。selected 每项 = [code, period, indicator]。"""
    plan: wizard_mod.IngestPlan
    selected: list[list[str]] = Field(default_factory=list)

    @field_validator("selected")
    @classmethod
    def _sanitize_selected(cls, v: list[list[str]]) -> list[list[str]]:
        # selected 每项 [code, period, indicator] 各自消毒
        cleaned = []
        for row in v:
            if len(row) >= 3:
                code = sanitize_mod.sanitize_text(row[0])
                period = sanitize_mod.sanitize_identifier(row[1])
                indicator = sanitize_mod.sanitize_text(row[2])
                cleaned.append([code, period, indicator])
        return cleaned


# --- 端点（verbatim from server.py，@app → @router） ---

@router.post("/api/ingest/plan")
def api_ingest_plan(req: IngestPlanRequest,
                    user: dict = Depends(auth_mod.current_user)) -> dict:
    """自然语言 → 需求单。LLM 只出草稿，认公司/认指标必过归一器（D2）。"""
    out = wizard_mod.parse_request(req.text)
    out["plan"] = out["plan"].model_dump()
    return out


@router.post("/api/ingest/preview")
def api_ingest_preview(plan: wizard_mod.IngestPlan,
                       user: dict = Depends(auth_mod.current_user)) -> dict:
    """需求单 → 抓取预览。只调采集不落库（"只抓不写"由 wizard 层用例守着）。"""
    return wizard_mod.preview(plan)


@router.post("/api/ingest/commit")
def api_ingest_commit(req: IngestCommitRequest,
                      user: dict = Depends(auth_mod.require_admin)) -> dict:
    """勾选格入库。actor 取自令牌（审计里的 sub），batch id 由 wizard 层生成。"""
    return wizard_mod.commit(req.plan, [tuple(s) for s in req.selected], actor=user)
