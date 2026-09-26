"""指标查询工具 —— 结构化财务数据的对外查询入口（只读）。

设计要点：

1. **返回值必须自带口径**。每个数都带 `unit` / `source`（`表.字段`）/ `period`。
   金融问答里"1741.44 亿"这种数字孤立出现等于没有信息 —— 用户要知道它来自哪张报表
   哪个字段，才能判断该不该信。数据层已把 `source_table` / `source_field` 随行存下，
   本层只需如实透出，**不美化、不合并**。

2. **公司名与指标名都要做归一，但绝不模糊猜测**。公司名支持简称包含匹配（"茅台"→贵州茅台），
   但一旦匹配到多家就返回 `ambiguous_company` + 候选列表，让上层（LLM）去澄清；
   指标名走 `config.resolve_indicator`，只认精确别名，"营业收入"绝不会被当成"营业总收入"。

3. **"查不到"要分级回答，不能一律报错**：
   - 指标名不认识 → `unknown_indicator`（带已知指标清单，便于模型自我纠正）
   - 公司认不出/有歧义 → `ambiguous_company` / `unknown_company`
   - 公司指标都认识、但库里没这个数 → `ok: true` + 空序列 + `note`（例如保险公司无毛利率，
     这是**正确结论**而不是故障；报错会误导用户以为系统坏了）

4. **跨公司对比时把"期次是否一致"暴露出来**。各公司最新年报期可能不同（有的已出 2025 年报、
   有的只到 2024），静默取"各自最新"再排名 = 拿不同年份比大小，是典型的数据事故。
   故返回值里必须带 `periods_consistent` 与每行的实际 `period`。
"""
from __future__ import annotations

from src import config, db
from src.tools.registry import tool

ANNUAL = "年报"

# 报告期类型别名：LLM 常给 "annual" / "年度" / "2024年报" 这类写法，统一归到库里的中文枚举。
REPORT_TYPE_ALIASES: dict[str, str] = {
    "年报": ANNUAL, "年度": ANNUAL, "年度报告": ANNUAL,
    "annual": ANNUAL, "year": ANNUAL, "y": ANNUAL, "fy": ANNUAL,
    "中报": "中报", "半年报": "中报", "半年度": "中报", "semi": "中报", "h1": "中报",
    "一季报": "一季报", "q1": "一季报",
    "三季报": "三季报", "q3": "三季报",
}


def _row_dict(row) -> dict:
    """sqlite3.Row / pymysql dict 统一成 dict（两后端同形是本项目的既定约定）。"""
    return dict(row) if isinstance(row, dict) else {k: row[k] for k in row.keys()}


def normalize_report_type(report_type: str | None) -> str:
    """报告期类型归一；认不出就退回年报（默认口径要在返回值里体现，不能悄悄换）。"""
    key = (report_type or "").strip()
    return REPORT_TYPE_ALIASES.get(key, REPORT_TYPE_ALIASES.get(key.lower(), ANNUAL))


def format_value(value: float | None, unit: str | None,
                 display_unit: str | None = None) -> str:
    """把原始值格式化成人类可读串（金额按 display_unit 换算，比率加 %）。

    这里只做**展示换算**，返回值里的 `value` 始终是原始单位（元），
    免得下游拿"亿元"当"元"再做二次换算。
    """
    if value is None:
        return ""
    if unit == "元":
        if display_unit == "亿元":
            return f"{value / 1e8:,.2f} 亿元"
        return f"{value:,.2f} 元"
    if unit == "%":
        return f"{value:,.2f}%"
    if unit == "倍":
        return f"{value:,.2f} 倍"
    return f"{value:,.2f} {unit or ''}".strip()


# ==================== 公司归一 ====================

def list_companies() -> list[dict]:
    """库内公司清单（工具层与 LLM 得先知道"有哪些公司"，否则必然编造公司名）。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT code, name, market, industry FROM companies ORDER BY code").fetchall()
    return [_row_dict(r) for r in rows]


def resolve_company(company: str, pool: list[dict] | None = None) -> dict:
    """公司名/代码 → `{ok, code, name, industry}`；歧义或找不到时给候选而不是猜。

    匹配优先级：代码精确 > 名称精确 > 名称包含。
    名称包含是便利（"茅台"→ 贵州茅台），但**只在一家命中时才接受**；
    命中多家一律报歧义，因为金融查询里选错公司比查不到严重得多。
    """
    q = (company or "").strip()
    if not q:
        return {"ok": False, "error": "empty_company", "message": "公司名/代码不能为空"}

    pool = list_companies() if pool is None else pool
    if not pool:
        return {"ok": False, "error": "empty_company_pool",
                "message": "公司库为空，请先执行取数入库（python -m src.ingest.fetch_eastmoney）"}

    for c in pool:                                    # 1) 代码精确
        if str(c.get("code")) == q:
            return {"ok": True, "code": c["code"], "name": c.get("name") or c["code"],
                    "industry": c.get("industry") or ""}

    lowered = q.lower()
    exact = [c for c in pool if (c.get("name") or "").lower() == lowered]
    if len(exact) == 1:                               # 2) 名称精确
        c = exact[0]
        return {"ok": True, "code": c["code"], "name": c["name"],
                "industry": c.get("industry") or ""}
    if len(exact) > 1:
        return {"ok": False, "error": "ambiguous_company", "query": q,
                "message": f"「{q}」精确匹配到多家公司，请用代码指定",
                "candidates": [{"code": c["code"], "name": c["name"]} for c in exact]}

    subs = [c for c in pool if lowered in (c.get("name") or "").lower()]
    if len(subs) == 1:                                # 3) 名称包含（唯一命中）
        c = subs[0]
        return {"ok": True, "code": c["code"], "name": c["name"],
                "industry": c.get("industry") or "",
                "matched_by": "partial_name"}
    if len(subs) > 1:
        return {"ok": False, "error": "ambiguous_company", "query": q,
                "message": f"「{q}」匹配到 {len(subs)} 家公司，请更精确或用代码指定",
                "candidates": [{"code": c["code"], "name": c["name"]} for c in subs]}

    return {"ok": False, "error": "unknown_company", "query": q,
            "message": f"公司库中没有与「{q}」匹配的公司",
            "available_companies": [{"code": c["code"], "name": c["name"]} for c in pool]}


# ==================== 底层查询 ====================

def _fetch_rows(code: str, indicator: str, report_type: str,
                periods: int | None = None,
                period: str | None = None) -> list[dict]:
    """查一个指标的序列（按期倒序）。periods / period 二选一。"""
    sql = ("SELECT period, report_type, indicator, value, unit, "
           "source_table, source_field FROM financial_indicators "
           "WHERE code=? AND indicator=? AND report_type=?")
    params: list = [code, indicator, report_type]
    if period:
        sql += " AND period=?"
        params.append(period)
    sql += " ORDER BY period DESC"
    if periods and not period:
        sql += " LIMIT ?"
        params.append(int(periods))
    with db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_dict(r) for r in rows]


def _indicator_payload(name: str) -> dict:
    """把 config 的口径定义转成对外 JSON（含别名与首选源，便于上层解释"用的是哪个口径"）。"""
    meta = config.indicator_meta(name) or {}
    return {
        "name": meta.get("name", name),
        "aliases": meta.get("aliases", []),
        "unit": meta.get("unit"),                 # 原始单位（元 / % / 元每股）
        "display_unit": meta.get("display_unit"),
        "statement": meta.get("statement"),
        "expected_source": (f"{meta['source_table']}.{meta['source_field']}"
                            if meta.get("source_table") else None),
    }


def _series_row(row: dict, meta: dict) -> dict:
    return {
        "period": row["period"],
        "value": row["value"],
        "display": format_value(row["value"], row.get("unit"),
                                meta.get("display_unit")),
        "source": f"{row.get('source_table')}.{row.get('source_field')}",
    }


def _counterpart_hint(std: str, code: str, report_type: str) -> dict | None:
    """指标对该公司不适用时，给出**口径替代项**及其值（而不是只说"没有"）。

    为什么要有这层：用户问「中国平安的营业成本」，标准答案是"保险业没有这个科目"，
    但这句单独说出等于把问题推回去。真实情况是——该科目在保险业有对应项「营业支出」，
    券商 App 上看到的多半就是它。所以这里顺手把对应项的最新值一起取出来，
    让回答变成"没有营业成本科目；对应的是营业支出 = 8,572.76 亿元（来源 …）"。

    注意：替代项**不能冒充**原指标 —— 返回值里它与 `indicator` 平级且带 `why`，
    工具描述也要求 LLM 转述时说明这是替代口径。
    """
    cp = (config.INDICATORS.get(std) or {}).get("counterpart")
    if not cp:
        return None
    out: dict = {"why": cp.get("why")}

    if cp.get("indicator"):
        alt = cp["indicator"]
        rows = _fetch_rows(code, alt, report_type, periods=1)
        out["indicator"] = {
            "name": alt,
            "latest": _series_row(rows[0], _indicator_payload(alt)) if rows else None,
        }

    if cp.get("ratio"):
        # 延迟导入：ratios 依赖本模块，模块级导入会形成环
        from src.tools import ratios as _ratios
        res = _ratios.calc_financial_ratio(code, cp["ratio"], report_type=report_type)
        out["ratio"] = {
            "name": cp["ratio"],
            "ok": res.get("ok"),
            "period": res.get("period"),
            "value": res.get("value"),
            "display": res.get("display"),
            "formula": (res.get("ratio") or {}).get("formula"),
            "note": (res.get("ratio") or {}).get("note"),
        }
    return out


# ==================== 工具 1：单指标序列 ====================

@tool(
    name="get_financial_indicator",
    description=(
        "查询**单家上市公司**某个财务指标的历年数值序列（默认最近 6 期年报）。\n"
        "适用：用户问某公司某指标是多少、近几年趋势如何。\n"
        "返回每个值都带单位与来源字段（source=报表.字段），可用于溯源核对。\n"
        "注意：\n"
        "- 指标名必须是标准口径名或已登记的别名（如「营业总收入」「归母净利润」「ROE」）。"
        "「营业收入」与「营业总收入」是两个不同口径，不能互换。\n"
        "- 该指标对此公司不适用时（例如保险公司没有「毛利率」「营业成本」）会返回空序列 + note，"
        "这是正确结论，不要当成系统故障反复重试。此时若该指标在适用行业存在**口径替代项**"
        "（如保险业的「营业支出」对应制造业的「营业成本」），返回值里的 counterpart 会带上"
        "替代项的最新值与来源 —— 转述给用户时必须**说明这是替代口径、原名不存在**，"
        "不能把替代项的值直接当成原指标回答。\n"
        "- 只返回库中已入库的数据；未入库的年份不会出现在结果里。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "company": {"type": "string",
                        "description": "公司 6 位代码（如 600519）或名称/简称（如 贵州茅台、茅台）"},
            "indicator": {"type": "string",
                          "description": "财务指标名称，如 营业总收入 / 营业收入 / 归母净利润 / 总资产 / ROE / 资产负债率"},
            "periods": {"type": "integer", "minimum": 1, "maximum": 20,
                        "description": "返回最近多少期，默认 6"},
            "report_type": {"type": "string",
                            "description": "报告期类型：年报（默认）/ 中报 / 一季报 / 三季报"},
        },
        "required": ["company", "indicator"],
    },
    returns="序列：{ok, company, indicator{口径}, series[{period, value, display, source}], latest, counterpart, note}",
)
def get_financial_indicator(company: str, indicator: str, periods: int = 6,
                            report_type: str | None = None) -> dict:
    """查单公司单指标的历年序列。"""
    std = config.resolve_indicator(indicator)
    if std is None:
        hint = config.AMBIGUOUS_ALIASES.get((indicator or "").strip())
        return {"ok": False, "error": "unknown_indicator", "indicator": indicator,
                "message": (hint or f"未登记的指标名「{indicator}」") +
                           "；请使用已知指标名", "known_indicators": sorted(config.INDICATORS)}

    comp = resolve_company(company)
    if not comp.get("ok"):
        return comp

    rt = normalize_report_type(report_type)
    rows = _fetch_rows(comp["code"], std, rt, periods=periods)
    meta = _indicator_payload(std)
    series = [_series_row(r, meta) for r in rows]

    note = None
    hint = None
    if not series:
        meta_cfg = config.INDICATORS[std]
        note = (f"库中无 {comp['name']} 的「{std}」{rt}数据。常见原因："
                f"1) 该指标对此行业不适用（{meta_cfg['statement']}口径，"
                f"如保险公司无营业成本/毛利率）；2) 该公司该期数据尚未取数入库。")
        hint = _counterpart_hint(std, comp["code"], rt)
        if hint:
            note += "该指标在适用行业中有**口径替代项**，见 counterpart（不可与原名混用）。"
    elif len(series) == 1 and periods and periods > 1:
        note = f"仅取到 1 期（请求 {periods} 期），库中该指标只有这么多期。"

    return {
        "ok": True,
        "company": {"code": comp["code"], "name": comp["name"],
                    "industry": comp.get("industry", "")},
        "indicator": meta,
        "report_type": rt,
        "count": len(series),
        "series": series,
        "latest": series[0] if series else None,
        "counterpart": hint,
        "note": note,
    }


# ==================== 工具 2：多公司对比 ====================

@tool(
    name="compare_companies",
    description=(
        "对**多家公司**按**同一个财务指标**做横向对比，按数值从大到小排名。\n"
        "适用：用户问「A 和 B 谁的营收高」「哪家 ROE 最高」「同行业对比」。\n"
        "默认各取最新一期年报，并在返回值中给出 period 与 periods_consistent：\n"
        "若各家期次不一致（有的已出 2025 年报、有的只到 2024），periods_consistent=false，"
        "此时结论必须说明'跨期次比较'，不能当作同口径排名。\n"
        "注意：对比跨越不同行业（如银行 vs 白酒）时资产负债率等指标不可直接比大小，"
        "返回值里的 industry 字段用于提示这一点。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "companies": {"type": "array", "items": {"type": "string"},
                          "minItems": 2,
                          "description": "要对比的公司列表（代码或名称，至少 2 家）"},
            "indicator": {"type": "string", "description": "对比用的财务指标名称"},
            "period": {"type": "string",
                       "description": "指定报告期（如 2024-12-31）；省略则各取最新一期年报"},
            "report_type": {"type": "string", "description": "报告期类型，默认年报"},
        },
        "required": ["companies", "indicator"],
    },
    returns="排名表：{ok, indicator, rows[{rank, code, name, period, value, display, source}], periods_consistent, missing}",
)
def compare_companies(companies: list[str], indicator: str,
                      period: str | None = None,
                      report_type: str | None = None) -> dict:
    """多公司同指标横向对比 + 排名。"""
    std = config.resolve_indicator(indicator)
    if std is None:
        return {"ok": False, "error": "unknown_indicator", "indicator": indicator,
                "message": f"未登记的指标名「{indicator}」",
                "known_indicators": sorted(config.INDICATORS)}

    if not companies or len(companies) < 2:
        return {"ok": False, "error": "need_at_least_two",
                "message": "对比至少需要 2 家公司"}

    rt = normalize_report_type(report_type)
    meta = _indicator_payload(std)
    pool = list_companies()

    rows: list[dict] = []
    errors: list[dict] = []
    missing: list[dict] = []

    for raw in companies:
        comp = resolve_company(raw, pool=pool)
        if not comp.get("ok"):
            errors.append({"input": raw, **comp})
            continue
        # 指定期则精确取；否则取最新一期（含 report_type 过滤，避免拿中报当年报）
        got = _fetch_rows(comp["code"], std, rt, periods=1, period=period)
        if not got:
            missing.append({"code": comp["code"], "name": comp["name"]})
            continue
        r = got[0]
        rows.append({
            "code": comp["code"], "name": comp["name"],
            "industry": comp.get("industry", ""),
            "period": r["period"], "value": r["value"],
            "display": format_value(r["value"], r.get("unit"), meta.get("display_unit")),
            "source": f"{r.get('source_table')}.{r.get('source_field')}",
        })

    rows.sort(key=lambda x: (x["value"] is None, -(x["value"] or 0)))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    periods = {r["period"] for r in rows}
    consistent = len(periods) <= 1

    notes: list[str] = []
    if rows and not consistent:
        notes.append(f"⚠️ 各公司期次不一致（{sorted(periods)}），属跨期次比较，"
                     f"结论需注明期次，不能当作同口径排名。")
    if missing:
        notes.append("以下公司在库中无该指标数据（可能该指标对其不适用）：" +
                     "、".join(f"{m['name']}({m['code']})" for m in missing))
    for e in errors:
        notes.append(f"「{e.get('input')}」无法解析：{e.get('message')}")

    return {
        "ok": bool(rows),
        "error": None if rows else "no_data",
        "indicator": meta,
        "report_type": rt,
        "period_requested": period,
        "periods_consistent": consistent,
        "rows": rows,
        "missing": missing,
        "errors": errors,
        "note": " ".join(notes) if notes else None,
    }


# ==================== 工具 3：可用范围自述 ====================

@tool(
    name="list_supported",
    description=(
        "列出当前**已入库的公司**与**可查询的指标/比率名**。\n"
        "何时用：不确定公司库里有哪些公司、或不确定某个指标名是否被支持时，"
        "先调用本工具再查询 —— 这样可以避免用库外公司或自造指标名去查、白跑一轮。\n"
        "不返回具体数值。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    returns="{ok, companies[], indicators[], ratios[]}",
)
def list_supported() -> dict:
    """返回库内公司与可用指标/比率清单。"""
    return {
        "ok": True,
        "companies": list_companies(),
        "indicators": sorted(config.INDICATORS),
        "ratios": sorted(config.RATIOS),
        "report_types": sorted(set(REPORT_TYPE_ALIASES.values())),
    }


if __name__ == "__main__":
    # 自检：python -m src.tools.indicators
    import json
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
    print(json.dumps(get_financial_indicator("贵州茅台", "营业总收入", periods=3),
                     ensure_ascii=False, indent=2))
    print(json.dumps(compare_companies(["贵州茅台", "五粮液", "宁德时代"], "ROE"),
                     ensure_ascii=False, indent=2))
