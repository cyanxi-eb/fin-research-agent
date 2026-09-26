"""多公司同指标对比（服务层） —— 薄封装：只做「工具返回 → 前端结构」的转换。

为什么单独一层，而不是让 `/api/compare` 直接调工具：

1. **工具是给 LLM 看的接口，前端是给图看的接口，两者不该耦合**。工具返回里
   `indicator` 是一坨口径元信息（别名 / 单位 / 来源表），`rows` 是带 `rank/display` 的
   排名表；而前端对比表与趋势图要的是稳定的扁平字段（每行 `code/name/period/value/unit/source`）
   与 `chart`。把转换收在一个纯函数里，HTTP 面与后续脚本面就不必各自拼一遍。
2. **取数逻辑只有一份**。本模块**不碰数据库**，只用 `registry.get_tool(...)` 取到
   已注册工具后调用 —— 谁都不许在这里再写一条 SQL，否则排名/序列口径迟早与工具分叉。
3. **排名与趋势是两件事，用两个工具**：
   - `rows`（排名）来自 `compare_companies` —— 它按"各自最新一期"给名次，口径不能变；
   - `chart.series`（趋势）来自 `get_financial_indicator` —— `compare_companies` 内部
     写死只取最新一期（每公司一个点），直接拿它画趋势会退化成孤点，所以序列必须
     逐公司另取**多期**。这也正是本模块存在的第二个理由。
4. **跨期次比较必须显式**。工具给了 `periods_consistent`，但前端要的是一句人话：
   **哪家是哪一期**。这里把它拼成 `note`，让"拿不同年份比大小"无法被静默忽略。

契约（`tests/test_compare.py` 固定）：
`compare(indicator, codes, *, period=None, db_path=None) -> dict`
- 返回键固定 `ok/indicator/unit/rows/periods_consistent/chart/note`（错误时另带 `error`）；
- `rows` 每行含 `code/name/period/value/unit/source`（排名来自 `compare_companies`）；
- `chart.series = [{code, name, points: [{period, value}]}]`，`points` 按期次**升序**；
- `chart.periods` = 各系列期次的全局有序并集（升序）；
- `period=None` → 覆盖库里**全部可用期次**；显式 `period=` → 各系列退化为该期单点，
  且 `chart.periods == [period]`；
- 指标不认识 → `ok=False, error="unknown_indicator"`；
- 公司数 < 2 → `ok=False, error="need_at_least_two_companies"`。
"""
from __future__ import annotations

from src import config
from src.tools import registry
from src.tools import indicators as _indicators  # noqa: F401  导入即注册两个工具

# get_financial_indicator 的 schema 里 periods 上限为 20（工具的对外契约），
# 用它取"尽可能全"的期次序列；超出部分属于工具接口不支持的范围。
_MAX_PERIODS = 20


def _empty_chart() -> dict:
    return {"series": [], "periods": []}


def _error(indicator: str, code: str, message: str) -> dict:
    """失败时的固定骨架：字段齐全，前端不必为错误分支单独判空。"""
    return {"ok": False, "error": code, "indicator": indicator, "unit": None,
            "rows": [], "periods_consistent": True, "chart": _empty_chart(),
            "note": message}


def _multi_points(code: str, indicator: str) -> list[dict] | None:
    """用 `get_financial_indicator` 取一家公司的多期序列（升序）。

    工具返回的 `series` 是**按期倒序**的，这里翻成升序（趋势图横轴由旧到新）。
    取不到（工具缺失 / 该公司无数据）返回 None，由调用方退回"最新一期"单点。
    """
    spec = registry.get_tool("get_financial_indicator")
    if spec is None:
        return None
    res = spec["function"](code, indicator, periods=_MAX_PERIODS)
    if not res.get("ok"):
        return None
    points = [{"period": s.get("period"), "value": s.get("value")}
              for s in (res.get("series") or [])]
    points.sort(key=lambda p: p["period"] or "")
    return points


def compare(indicator: str, codes: list[str], *, period: str | None = None,
            db_path=None) -> dict:
    """多公司对比：`rows` 走 compare_companies 排名，`chart` 逐公司取多期序列。

    `db_path` 仅用于测试/脚本指定库；省略则沿用 `config.DB_PATH`（工具层在调用时读它）。
    """
    companies = [str(c).strip() for c in (codes or []) if str(c).strip()]
    if len(companies) < 2:
        return _error(indicator, "need_at_least_two_companies",
                      "对比至少需要 2 家公司")

    compare_spec = registry.get_tool("compare_companies")
    if compare_spec is None:                # 只可能因导入顺序出错，不能静默当成"没数据"
        return _error(indicator, "tool_unavailable",
                      "工具 compare_companies 未注册")

    old_path = config.DB_PATH
    if db_path is not None:
        config.DB_PATH = db_path
    try:
        res = compare_spec["function"](companies, indicator, period=period)

        # 指标不认识时工具返回的 `indicator` 是**输入字符串**（不是口径 dict），必须分辨，
        # 否则下面 .get("unit") 会炸 —— 这正是"两个接口返回形态不同"的典型坑。
        raw_meta = res.get("indicator")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        unit = meta.get("unit")

        rows = [
            {
                "code": r.get("code"), "name": r.get("name"), "period": r.get("period"),
                "value": r.get("value"), "unit": unit, "source": r.get("source"),
                "rank": r.get("rank"), "display": r.get("display"),
            }
            for r in (res.get("rows") or [])
        ]
        consistent = bool(res.get("periods_consistent", True))

        # ---- 趋势序列：显式 period → 单点；否则逐公司取多期 ----
        series: list[dict] = []
        for r in rows:
            if period is not None:
                points = [{"period": r["period"], "value": r["value"]}]
            else:
                points = _multi_points(r["code"], indicator) or [
                    {"period": r["period"], "value": r["value"]}]
            series.append({"code": r["code"], "name": r["name"], "points": points})

        if period is not None:
            chart_periods = [period]
        else:
            chart_periods = sorted(
                {p["period"] for s in series for p in s["points"]})

        notes: list[str] = []
        if rows and not consistent:
            detail = "、".join(f"{r['name']}({r['code']}) 为 {r['period']}" for r in rows)
            notes.append(f"各公司期次不一致（{detail}），属跨期次比较，不能当作同口径排名。")
        missing = res.get("missing") or []
        if missing:
            notes.append("库中无该指标数据：" +
                         "、".join(f"{m.get('name')}({m.get('code')})" for m in missing))
        errors = res.get("errors") or []
        if errors:
            notes.append("输入无法解析：" + "、".join(str(e.get("input")) for e in errors))

        out = {
            "ok": bool(res.get("ok")),
            "indicator": meta.get("name") or indicator,
            "unit": unit,
            "rows": rows,
            "periods_consistent": consistent,
            "chart": {"series": series, "periods": chart_periods},
            "note": " ".join(notes) if notes else (res.get("note") or res.get("message") or None),
        }
        if res.get("error"):
            out["error"] = res["error"]
        return out
    finally:
        config.DB_PATH = old_path