"""比率计算工具 —— 派生指标，**返回完整计算过程**（分子、分母、每一分项及其来源）。

为什么"能算"还不够，必须"把算式摊开"：
财务比率是同一句话对应多套算法的重灾区。用户问"茅台毛利率是多少"，
他要的不只是 91.93% 这个数，而是"这个数怎么来的、用的是哪个口径" ——
因为年报、券商软件、Wind、东财口径常常不同，差 1 个百分点就足以推翻一个投资结论。
本工具因此返回：
  - `numerator` / `denominator`：各自的**分项列表**（指标名、符号、原始值、单位、来源字段）
  - `formula`：人读的公式
  - `official`：东财官方口径值（若该比率有官方口径）
  - `cross_check`：派生值 vs 官方值的差与判读
  - `note`：口径边界说明（必须随结果返回，不能只写进代码注释）

这样任何一个数都能被逐项核对：分母是不是我理解的那个？差的那 0.9 个百分点从哪来？
—— 这正是本项目「可验证引用」在数值侧的落地。
"""
from __future__ import annotations

from src import config, db
from src.tools.indicators import (
    _indicator_payload,
    format_value,
    normalize_report_type,
    resolve_company,
)
from src.tools.registry import tool

# 派生值 vs 官方值的差异判读阈值（单位：百分点 / 倍）。
# 设为 0.3 / 2.0 的依据：同口径（毛利率、资产负债率）实测差 ~0.00pp；
# 不同口径但量级可比（ROE 期末 vs 加权）实测差 ~1.0pp。所以 0.3 以内判"一致"，
# 2.0 以内判"口径差异"，超过 2.0 就得提示"需人工复核"——可能是数据源不同期或口径混杂。
_TOL_SAME = 0.3
_TOL_CLOSE = 2.0


def _rows_for(code: str, indicators: list[str], report_type: str) -> dict[str, dict[str, dict]]:
    """一次性取某公司在给定指标上的全部期次 → `{period: {indicator: row}}`。

    为什么一次查完再分组，而不是每个指标查一次：比率的分母与分子必须**同一期**，
    若逐指标查、再各自取"最新"，遇到某指标缺一期就会用不同期的数算出个看着正常的错值
    （静默错误）。一次取回后按 period 对齐，缺项能被显式检出。
    """
    marks = ", ".join(["?"] * len(indicators))
    sql = (f"SELECT period, indicator, value, unit, source_table, source_field "
           f"FROM financial_indicators WHERE code=? AND report_type=? "
           f"AND indicator IN ({marks})")
    with db.get_conn() as conn:
        rows = conn.execute(sql, [code, report_type, *indicators]).fetchall()

    by_period: dict[str, dict[str, dict]] = {}
    for r in rows:
        d = dict(r) if isinstance(r, dict) else {k: r[k] for k in r.keys()}
        if d["value"] is None:
            continue
        by_period.setdefault(d["period"], {})[d["indicator"]] = d
    return by_period


def _terms(pairs: list[tuple[str, int]], bucket: dict[str, dict]) -> tuple[float | None, list[dict]]:
    """按 [(指标, 符号)] 求和；任一分项缺失即返回 None（不允许"缺项当 0"）。"""
    total = 0.0
    detail: list[dict] = []
    for ind, sign in pairs:
        row = bucket.get(ind)
        if not row:
            return None, detail
        meta = _indicator_payload(ind)
        total += sign * float(row["value"])
        detail.append({
            "indicator": ind,
            "sign": "+" if sign > 0 else "−",
            "value": row["value"],
            "unit": row.get("unit"),
            "display": format_value(row["value"], row.get("unit"), meta.get("display_unit")),
            "source": f"{row.get('source_table')}.{row.get('source_field')}",
        })
    return total, detail


def _fmt(value: float, unit: str | None) -> str:
    if unit == "%":
        return f"{value:,.2f}%"
    if unit == "倍":
        return f"{value:,.2f} 倍"
    return f"{value:,.4f}"


def _alternatives(std: str, code: str, report_type: str,
                  period: str | None = None) -> list[dict]:
    """本比率算不出来时，把登记过的**替代口径**也算一遍（只报结果摘要，不递归展开）。

    为什么值得多算一次：用户问「中国平安毛利率」，只回"保险业无此口径"是把问题推回去；
    直接把营业支出口径的数叫「毛利率」又是在造假。两难的正解是**给替代口径 + 标明名字与公式**，
    用户自己决定要不要用。`alternatives` 在 config.RATIOS 里声明，这里只负责执行与摘要。
    不递归（替代口径的 alternatives 不再展开），避免链式爆炸。
    """
    out: list[dict] = []
    for alt_name in (config.RATIOS.get(std) or {}).get("alternatives", []):
        res = calc_financial_ratio(code, alt_name, period=period, report_type=report_type)
        out.append({
            "ratio": alt_name,
            "ok": res.get("ok"),
            "period": res.get("period"),
            "value": res.get("value"),
            "display": res.get("display"),
            "formula": (res.get("ratio") or {}).get("formula"),
            "note": (res.get("ratio") or {}).get("note"),
            "reason_if_failed": None if res.get("ok") else res.get("message"),
        })
    return out


@tool(
    name="calc_financial_ratio",
    description=(
        "计算**派生财务比率**（毛利率 / 毛利率(保险口径) / 净利率 / 资产负债率 / ROE / 经营现金流净利润比），"
        "并返回完整计算过程：分子、分母的每个分项（含取值与来源字段）。\n"
        "何时用：用户问的是比率本身（如「毛利率多少」「ROE 高不高」）。\n"
        "何时不用：用户要的是报表原始科目（营收、总资产…）→ 用 get_financial_indicator；\n"
        "            用户要跨公司比同一个比率 → 用 compare_companies 分别取分子分母后自行比较，\n"
        "            或对每家公司各调一次本工具。\n"
        "返回的 official 字段是东财官方口径值，cross_check 给出派生值与官方值的差异判读 —— "
        "两者不一致通常代表**口径不同**（例如 ROE 期末口径 vs 加权平均口径），"
        "这属于有效信息，应如实转述，不要把它说成'数据有误'。\n"
        "任一分项缺失时（例如保险公司无「营业成本」，无法算毛利率）返回 ok=false、指出缺哪项，"
        "并在 alternatives 里给出**同义替代口径**的结果（如保险业的「毛利率(保险口径)」）——"
        "转述时必须说明那是替代口径、与原名不是同一个东西，不能直接当成原名回答。\n"
        "不要用 0 代替缺失项。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "company": {"type": "string", "description": "公司 6 位代码或名称/简称"},
            "ratio": {"type": "string",
                      "description": "比率名：毛利率 / 毛利率(保险口径) / 净利率 / 资产负债率 / ROE / 经营现金流净利润比"},
            "period": {"type": "string",
                       "description": "报告期（如 2024-12-31）；省略则取该公司分项最齐的最新一期"},
            "report_type": {"type": "string", "description": "报告期类型，默认年报"},
        },
        "required": ["company", "ratio"],
    },
    returns="{ok, company, ratio{formula,note}, period, numerator{terms,value}, denominator{...}, value, official, cross_check, alternatives}",
)
def calc_financial_ratio(company: str, ratio: str, period: str | None = None,
                         report_type: str | None = None) -> dict:
    """算派生比率，返回可审计的计算过程。"""
    std = config.resolve_ratio(ratio)
    if std is None:
        return {"ok": False, "error": "unknown_ratio", "ratio": ratio,
                "message": f"未登记的比率名「{ratio}」",
                "known_ratios": sorted(config.RATIOS)}

    comp = resolve_company(company)
    if not comp.get("ok"):
        return comp

    meta = config.ratio_meta(std)
    rt = normalize_report_type(report_type)
    needed = meta["needed_indicators"]
    by_period = _rows_for(comp["code"], needed, rt)

    # 缺项时把"可能是行业不适用"也说清楚：否则用户分不清「保险公司本来就没有毛利率」
    # 与「数据还没入库」——前者是结论，后者是待办，补救动作完全不同。
    applicability = (f"注：{needed} 对「{comp.get('industry') or '该公司'}」这类公司可能本就不适用"
                     f"（如保险公司无营业成本/毛利率），也可能只是尚未取数入库。")

    def base(payload: dict) -> dict:
        return {
            "company": {"code": comp["code"], "name": comp["name"],
                        "industry": comp.get("industry", "")},
            "ratio": {"name": std, "aliases": meta["aliases"], "formula": meta["formula"],
                      "unit": meta["unit"], "note": meta["note"]},
            "report_type": rt,
            **payload,
        }

    # ---- 选期：指定则用指定的；否则取"分项最齐"的最新一期 ----
    period_note = None
    if period:
        bucket = by_period.get(period) or {}
        if not bucket:
            return {"ok": False, "error": "no_data", "period": period,
                    "needed_indicators": needed,
                    "message": f"{comp['name']} 在 {period}（{rt}）无 {needed} 任一数据。"
                               + applicability,
                    "alternatives": _alternatives(std, comp["code"], rt, period),
                    **base({})}
        chosen = period
    else:
        if not by_period:
            return {"ok": False, "error": "no_data",
                    "needed_indicators": needed,
                    "message": f"库中无 {comp['name']} 的 {needed} 数据（{rt}）。" + applicability,
                    "alternatives": _alternatives(std, comp["code"], rt),
                    **base({})}
        # 先按"分项齐不齐"排序，再按期次，取"最齐且最新"的一期。
        # 为什么不是简单取最新一期：新出的年报未必各分项都已入库（例如刚补了营业收入、
        # 还没补营业成本），此时取最新期会直接算不出来或算出一个缺项的错值。
        chosen = max(sorted(by_period), key=lambda p: (len(by_period[p]), p))
        latest = max(by_period)
        if chosen != latest:
            lack = [ind for ind in needed if ind not in by_period[latest]]
            period_note = (f"最新一期 {latest} 缺少 {lack}，故选用分项最齐且次新的 "
                           f"{chosen}。若要指定期次请显式传 period。")
    bucket = by_period[chosen]

    missing = [ind for ind in needed if ind not in bucket]
    if missing:
        return {
            "ok": False, "error": "insufficient_components",
            "period": chosen,
            "message": (f"算「{std}」需要 {needed}，但 {comp['name']} {chosen}（{rt}）"
                        f"缺 {missing}。不能用 0 代替缺失项——请改用其他口径或其他公司。"),
            "needed_indicators": needed, "missing_indicators": missing,
            "period_note": period_note,
            "alternatives": _alternatives(std, comp["code"], rt, chosen),
            **base({}),
        }

    num, num_terms = _terms(meta["numerator"], bucket)
    den, den_terms = _terms(meta["denominator"], bucket)
    if not num or den is None:  # pragma: no cover —— 上面已校验缺失，这里兜底防 0 分母
        return {"ok": False, "error": "compute_failed", "message": "分子/分母计算失败",
                "needed_indicators": needed, **base({})}
    if den == 0:
        return {"ok": False, "error": "zero_denominator",
                "message": f"分母为 0（{meta['denominator']}），无法计算比率",
                **base({})}

    raw = num / den
    value = raw * 100 if meta["unit"] == "%" else raw
    payload = {
        "period": chosen,
        "period_note": period_note,
        "numerator": {"value": num, "terms": num_terms,
                      "display": format_value(
                          num, "元" if any(t["unit"] == "元" for t in num_terms) else None)},
        "denominator": {"value": den, "terms": den_terms,
                        "display": format_value(
                            den, "元" if any(t["unit"] == "元" for t in den_terms) else None)},
        "value": value,
        "display": _fmt(value, meta["unit"]),
    }

    # ---- 官方口径交叉对账 ----
    official = None
    cross = None
    off_ind = meta.get("official_indicator")
    if off_ind:
        got = by_period.get(chosen, {})
        if off_ind not in got:
            with db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT period, value, unit, source_table, source_field "
                    "FROM financial_indicators WHERE code=? AND report_type=? "
                    "AND indicator=? AND period=?",
                    (comp["code"], rt, off_ind, chosen)).fetchall()
            for r in rows:
                d = dict(r) if isinstance(r, dict) else {k: r[k] for k in r.keys()}
                if d["value"] is not None:
                    got[off_ind] = d
        row = got.get(off_ind)
        if row:
            official = {
                "indicator": off_ind, "value": float(row["value"]),
                "display": _fmt(float(row["value"]), meta["unit"]),
                "source": f"{row.get('source_table')}.{row.get('source_field')}",
            }
            delta = value - official["value"]
            if abs(delta) <= _TOL_SAME:
                verdict = "一致"
            elif abs(delta) <= _TOL_CLOSE:
                verdict = "口径差异（量级一致，勿判为错误）"
            else:
                verdict = "差异较大，需人工复核口径或数据期次"
            cross = {"delta": delta,
                     "delta_display": f"{delta:+.2f}{'pp' if meta['unit'] == '%' else meta['unit']}",
                     "verdict": verdict}

    payload["official"] = official
    payload["cross_check"] = cross
    return {"ok": True, "error": None, **base(payload)}


if __name__ == "__main__":
    # 自检：python -m src.tools.ratios
    import json
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
    for r in ("毛利率", "ROE", "资产负债率"):
        print(f"\n### {r}")
        print(json.dumps(calc_financial_ratio("贵州茅台", r), ensure_ascii=False, indent=2))
