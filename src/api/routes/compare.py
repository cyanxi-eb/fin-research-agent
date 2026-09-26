"""对比端点（Phase 0-2 逐路由组迁移第二批）。

**纯 verbatim copy**：从 server.py 复制过来，不改任何逻辑。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src import auth as auth_mod
from src import compare as compare_mod

router = APIRouter()


# --- CompareRequest Pydantic 模型（verbatin from server.py） ---

class CompareRequest(BaseModel):
    """多公司对比入参（POST 形态）。`codes` 至少 2 家 —— 单家没有"对比"语义。"""

    indicator: str = Field(..., min_length=1, description="财务指标名，如 营业总收入")
    codes: list[str] = Field(..., min_length=2, description="要对比的公司代码或名称，至少 2 家")
    period: str | None = Field(
        None, description="指定报告期（如 2024-12-31）；留空则各取最新一期年报")


# --- 辅助函数（verbatim from server.py） ---

def _compare_response(indicator: str, codes: list[str], period: str | None) -> dict:
    """对比端点的公共实现：GET 与 POST 走同一条路径，避免两套行为漂移。

    参数错（指标不认识 / 公司不足 2 家）是 **400**；"库里没这个数"是**结论**不是错误，
    仍返回 200 并由 `ok=false` + `note` 表达（与拒答的语义一致）。
    """
    result = compare_mod.compare(indicator, codes, period=period)
    if not result.get("ok") and result.get("error") in (
            "unknown_indicator", "need_at_least_two_companies"):
        raise HTTPException(status_code=400,
                            detail=result.get("note") or result["error"])
    return result


# --- 端点（verbatim from server.py，@app → @router） ---

@router.get("/api/compare")
def api_compare_get(
    indicator: str = Query(..., description="财务指标名，如 营业总收入"),
    codes: str = Query(..., description="逗号分隔的公司代码或名称，至少 2 家"),
    period: str | None = Query(
        None, description="指定报告期（如 2024-12-31）；留空则各取最新一期年报"),
    user: dict = Depends(auth_mod.current_user),
) -> dict:
    """多公司同指标横向对比（GET 形态，便于前端与人工验证）。

    返回结构固定为 `{ok, indicator, unit, rows, periods_consistent, chart, note}`：
    `rows` 供对比表，`chart.series` 供前端手绘趋势图。跨期次时 `periods_consistent=false`
    且 `note` 写明各家实际期次 —— 前端必须在表头显示该告警。
    """
    parsed = [c.strip() for c in codes.split(",") if c.strip()]
    return _compare_response(indicator, parsed, period)


@router.post("/api/compare")
def api_compare_post(req: CompareRequest,
                     user: dict = Depends(auth_mod.current_user)) -> dict:
    """同 `GET /api/compare`，POST 形态便于前端直接提交数组。"""
    return _compare_response(req.indicator, req.codes, req.period)
