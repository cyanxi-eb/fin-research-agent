"""数值分析子图 —— **强制走工具层**，数值不允许 LLM 从原文读。

## 为什么这条路必须与 RAG 分开

年报原文里同一个指标会出现多次，且**口径不同**：合并 vs 母公司、本期 vs 上年同期、
期末 vs 加权平均。"从原文读一个数"在金融场景里是**静默错误的高发区** ——
多数时候抄对了，少数时候抄到"上年同期"或"母公司口径"，而这两种错**看起来完全正常**。
所以数值不走检索、不走模型，只走 `src/tools/`（Step 2 已保证：只读、每个值自带
`source=报表.字段`）。

## 零幻觉靠三件事同时成立（不是靠提示词）

1. **数值来源唯一**：本模块里所有数字都来自 `registry.dispatch()` 的返回，
   模型只可能"组织句子"，无从"读数"。
2. **答案组装是确定性的**：默认由 `compose()` 按模板拼出来，不经过模型 ——
   数值题的核心价值是"数对 + 可溯源"，不是"句子漂亮"，而确定性让校验可以做到精确。
3. **下游还有一道数字回归**（`graph/verify.py`）：答案里每个数字都要能在工具返回里找到，
   找不到就进 HITL。所以即使将来加了"模型润色"，也拦得住它顺手改数。

## 工具编排（有意的简单）

| 情形 | 调用 |
|---|---|
| 1 家公司 + 比率 | `calc_financial_ratio`（带公式与官方口径对账） |
| 1 家公司 + 科目 | `get_financial_indicator`（序列，按年份挑行） |
| N 家公司 + 科目 | `compare_companies`（自带排名与期次一致性检查） |
| N 家公司 + 比率 | 每家公司各调一次 `calc_financial_ratio`，再自行横向排（工具不做比率对比） |

比率优先于科目：同一名字（如"毛利率"）在库中既是原始指标也是派生比率，
**比率工具给出的信息严格更多**（公式 + 分子分母分项 + 官方口径交叉对账）。
"""
from __future__ import annotations

from src import config
from src.graph.state import QAState
from src.retrieve.bm25 import content_terms
from src.tools import registry
from src.tools.indicators import list_companies, resolve_company

# 工具名常量（拼错工具名会被 dispatch 兜成 unknown_tool，但那时代码已经跑偏了）
T_INDICATOR = "get_financial_indicator"
T_COMPARE = "compare_companies"
T_RATIO = "calc_financial_ratio"

RATIO_NAMES = frozenset(config.RATIOS)


# ==================== 从问题里抽取实体 ====================

def detect_companies(question: str, pool: list[dict] | None = None) -> list[dict]:
    """从问题里识别公司（**复用工具层的归一逻辑**，不另写一套匹配规则）。

    做法：把问题切成实词，逐个丢给 `resolve_company`；命中唯一公司的收下，
    歧义或认不出的丢掉。为什么用实词而不是子串扫描：子串扫描会让
    「中国」这种词命中一大片公司，然后不得不靠"命中最多者"来猜 ——
    而金融查询里**猜错公司比查不到严重得多**（拿到别家的数还以为是对的）。

    歧义/未知一律**不猜**：宁可返回空（由上层告知"未能识别公司"并列出可选公司），
    也不给一个可能张冠李戴的数。
    """
    try:
        pool = list_companies() if pool is None else pool
    except Exception:                                # noqa: BLE001 —— 库不可用时不阻断路由
        return []

    found: dict[str, dict] = {}
    for term in content_terms(question or ""):
        if not term:
            continue
        res = resolve_company(term, pool=pool)
        if res.get("ok"):
            found.setdefault(res["code"], {
                "code": res["code"], "name": res["name"],
                "industry": res.get("industry", ""), "matched_by": res.get("matched_by", "exact"),
            })
    # 也接受问题里直接写 6 位代码
    for term in content_terms(question or ""):
        if term.isdigit() and len(term) == 6:
            res = resolve_company(term, pool=pool)
            if res.get("ok"):
                found.setdefault(res["code"], {
                    "code": res["code"], "name": res["name"],
                    "industry": res.get("industry", ""), "matched_by": "code",
                })
    return list(found.values())


def detect_year(question: str) -> int | None:
    """问题里显式写的年份（最后一个 4 位数，19xx/20xx）。"""
    import re

    ys = [int(m.group(0)) for m in re.finditer(r"(?<!\d)(19|20)\d{2}(?!\d)", question or "")]
    return ys[-1] if ys else None


def resolve_names(question: str, matched: dict | None) -> tuple[list[str], list[str]]:
    """把问句里的指标说法解析成 `(比率名, 科目名)` 两个标准名列表。

    别名表与路由用的是**同一份** `config`，所以这里解析不出来基本只可能是编码问题，
    不是"路由认识、工具不认识"那种漂移。

    ⚠️ `matched` 为空时**要自己从问句里再抽一遍**，不能只看路由给的结论。
    踩过的坑：`force_intent="analysis"`（API 显式指定意图）时路由节点没有跑，
    `matched` 是空的，于是本子图报"未能识别库内指标名"—— 明明问句里写着"营业总收入"。
    根因是把"该查什么"耦合到了"路由跑了没跑"上。指标名是问句本身的属性，
    与谁决定走哪条链路无关，所以这里独立地再抽一次。
    """
    names = list(((matched or {}).get("indicators") or []) if isinstance(matched, dict) else [])
    if not names:
        from src.graph.router import indicator_aliases

        ql = (question or "").lower()
        names = [n for n in indicator_aliases() if n and n.lower() in ql]

    ratios: list[str] = []
    inds: list[str] = []
    for n in names:
        std_r = config.resolve_ratio(n)
        std_i = config.resolve_indicator(n)
        if std_r and std_r not in ratios:
            ratios.append(std_r)
        elif std_i and std_i not in inds:
            inds.append(std_i)
    return ratios, inds


# ==================== 编排与执行 ====================

def plan_calls(companies: list[dict], ratios: list[str], inds: list[str],
               year: int | None) -> list[dict]:
    """生成工具调用计划（纯函数，可单测：断言"什么问句该调哪些工具"）。"""
    period = f"{year}-12-31" if year else None
    calls: list[dict] = []
    names = [c["name"] for c in companies]

    if not companies:
        return calls

    for r in ratios:
        if len(companies) >= 2:
            for c in companies:
                calls.append({"name": T_RATIO,
                              "arguments": {"company": c["name"], "ratio": r, **(
                                  {"period": period} if period else {})}})
        else:
            calls.append({"name": T_RATIO,
                          "arguments": {"company": names[0], "ratio": r, **(
                              {"period": period} if period else {})}})

    for i in inds:
        if len(companies) >= 2:
            calls.append({"name": T_COMPARE,
                          "arguments": {"companies": names, "indicator": i, **(
                              {"period": period} if period else {})}})
        else:
            calls.append({"name": T_INDICATOR,
                          "arguments": {"company": names[0], "indicator": i}})
    return calls


def _summarize(name: str, res: dict) -> str:
    """把工具返回压成一行摘要（进 tool_calls 记录，供审计与前端展示）。"""
    if not res.get("ok"):
        return f"失败：{res.get('error')} —— {res.get('message')}"
    if name == T_INDICATOR:
        series = res.get("series") or []
        if not series:
            return (f"{res['company']['name']} 无「{res['indicator']['name']}」数据"
                    + (f"（{res.get('note')}）" if res.get("note") else ""))
        # 列前 3 期而不是只报"最新"：问题里带了年份时答案用的是那一年，
        # 只报最新会让摘要与答案对不上（看起来像答案用错了期次），白白引起怀疑。
        head = "；".join(f"{r['period']}={r['display']}" for r in series[:3])
        return (f"{res['company']['name']} 的「{res['indicator']['name']}」"
                f"共 {res.get('count')} 期：{head}" + ("…" if len(series) > 3 else ""))
    if name == T_COMPARE:
        rows = res.get("rows") or []
        head = "；".join(f"{r['name']} {r['display']}" for r in rows[:4])
        return f"对比 {len(rows)} 家：{head}" + (
            "（⚠️ 期次不一致）" if rows and not res.get("periods_consistent") else "")
    if name == T_RATIO:
        return (f"{res['company']['name']}「{res['ratio']['name']}」"
                f"{res.get('period')} = {res.get('display')}")
    return str(res)[:120]


def _sources(name: str, res: dict) -> list[str]:
    """从工具返回里抽出 `表.字段` 溯源串（去重）。"""
    out: list[str] = []
    if name == T_INDICATOR:
        for row in res.get("series") or []:
            if row.get("source"):
                out.append(f"{row['period']}:{row['source']}")
    elif name == T_COMPARE:
        for row in res.get("rows") or []:
            if row.get("source"):
                out.append(f"{row['period']}:{row['source']}")
    elif name == T_RATIO:
        for side in ("numerator", "denominator"):
            for t in ((res.get(side) or {}).get("terms") or []):
                if t.get("source"):
                    out.append(f"{res.get('period')}:{t['source']}")
        off = res.get("official") or {}
        if off.get("source"):
            out.append(f"official:{off['source']}")
    return list(dict.fromkeys(out))


def run_plan(plan: list[dict]) -> list[dict]:
    """执行计划。返回 `tool_calls` 记录（名/参/结果/摘要/来源/原始返回）。"""
    records: list[dict] = []
    for call in plan:
        res = registry.dispatch(call["name"], call["arguments"])
        records.append({
            "name": call["name"],
            "arguments": call["arguments"],
            "ok": bool(res.get("ok")),
            "error": res.get("error"),
            "summary": _summarize(call["name"], res),
            "sources": _sources(call["name"], res),
            "result": res,
        })
    return records


# ==================== 确定性组句 ====================

# 前端按**纯文本**渲染答案（textContent）：`**` 星号、`> ` 引用记号会原样露出。
# 所以组句里不写任何 markdown 记号 —— "句子漂亮"不是这条链路的目标，"干净可读"是。

# 与 config.DATA_SOURCES 的 source_key 全集对齐（dc_*/sina_* 是补充源，答案里注明）。
# 审计里仍是精确的 表.字段；这里只负责"人一眼能读懂出自哪张表"。
_REPORT_NAMES = (
    ("income", "合并利润表"),
    ("balance", "合并资产负债表"),
    ("cashflow", "合并现金流量表"),
    ("main", "主要财务指标"),
    ("dc_income", "利润表（补充源）"),
    ("dc_balance", "资产负债表（补充源）"),
    ("dc_cashflow", "现金流量表（补充源）"),
    ("sina_income", "利润表（新浪源）"),
    ("sina_balance", "资产负债表（新浪源）"),
)


def _human_source(field: str) -> str:
    """`表.字段` 溯源串 → 人话报表名（`income.TOTAL_OPERATE_INCOME` → 合并利润表）。

    字段名本身仍留在 `tool_calls.sources`（审计/程序核对用），答案里只给人话。
    认不出的前缀**原样返回** —— 宁可难看，不可误导。
    """
    s = str(field or "").strip()
    head = s.split(".", 1)[0].strip().lower()
    for prefix, label in _REPORT_NAMES:
        if head.startswith(prefix):
            return label
    return s


def _period_year(period) -> int | None:
    s = str(period or "")[:4]
    return int(s) if s.isdigit() else None


def _report_url(code, year) -> str | None:
    """年报原文 PDF 的巨潮链接（ingest 时 `fetch_cninfo` 记在 manifest 里的"原始出处"）。

    URL 是答案的**加分项**：manifest 缺失（未下载/手工放/容器精简）或任何意外都返回
    None —— 绝不能挡住数值本身，更不能为了"看起来完整"编一个链接。
    """
    if not code or not year:
        return None
    try:
        from src.ingest.fetch_cninfo import load_manifest
        entry = (load_manifest(str(code)) or {}).get(str(year)) or {}
        url = str(entry.get("url") or "").strip()
        return url or None
    except Exception:                                 # noqa: BLE001
        return None


def _maybe_url_line(code, period) -> str | None:
    y = _period_year(period)
    url = _report_url(code, y)
    return f"    原文：{url}" if url else None


def _fmt_ratio(call: dict) -> list[str]:
    res = call["result"]
    if not call["ok"]:
        return [f"- 「{call['arguments'].get('ratio')}」未能计算：{res.get('message')}"]
    lines = [f"{res['company']['name']}「{res['ratio']['name']}」"
             f"（{res.get('period')}，{res.get('report_type')}）= {res.get('display')}"]
    url_line = _maybe_url_line(res["company"].get("code"), res.get("period"))
    if url_line:
        lines.append(url_line)
    lines.append(f"    公式：{res['ratio']['formula']}")
    num, den = res.get("numerator") or {}, res.get("denominator") or {}
    for label, side in (("分子", num), ("分母", den)):
        terms = "；".join(
            f"{t['sign']} {t['indicator']} {t['display']}（来源：{_human_source(t['source'])}）"
            for t in (side.get("terms") or []))
        lines.append(f"    {label}：{terms}")
    if res.get("official"):
        off = res["official"]
        cross = res.get("cross_check") or {}
        lines.append(f"    官方口径对账：{off['indicator']} = {off['display']}"
                     f"（来源：{_human_source(off['source'])}），差异 {cross.get('delta_display')}，"
                     f"判读：{cross.get('verdict')}")
    if res.get("period_note"):
        lines.append(f"    期次说明：{res['period_note']}")
    if res.get("alternatives"):
        alts = "；".join(f"{a['ratio']}={a['display'] or '无'}" for a in res["alternatives"])
        lines.append(f"    可替代口径：{alts}（不是同一个指标，不可混用）")
    return lines


def _fmt_indicator(call: dict, year: int | None) -> list[str]:
    res = call["result"]
    if not call["ok"]:
        return [f"- 「{call['arguments'].get('indicator')}」未能查询：{res.get('message')}"]
    name = res["indicator"]["name"]
    series = res.get("series") or []
    if not series:
        lines = [f"- {res['company']['name']} 的「{name}」：库中无数据。"]
        if res.get("note"):
            lines.append(f"    说明：{res['note']}")
        cp = res.get("counterpart")
        if cp:
            if cp.get("indicator"):
                it = cp["indicator"]
                lines.append(f"    口径替代项：{it['name']} = "
                             f"{(it.get('latest') or {}).get('display')}"
                             f"（来源：{_human_source((it.get('latest') or {}).get('source'))}）")
            lines.append(f"    ⚠️ {cp.get('why')}——替代项不是原指标，转述时必须说明。")
        return lines

    rows = [r for r in series if not year or r["period"].startswith(str(year))] or series
    head = rows[0]
    lines = [f"{res['company']['name']}「{name}」"
             f"（{head['period']}，{res.get('report_type')}）= {head['display']}"
             f"（来源：{_human_source(head.get('source'))}）"]
    url_line = _maybe_url_line(res["company"].get("code"), head.get("period"))
    if url_line:
        lines.append(url_line)
    if len(rows) > 1:
        # 其余期次与主行同源（同公司同指标同报表），来源在主行已标注，不逐行复读
        lines.append("    其余期次：" + "；".join(
            f"{r['period']} {r['display']}" for r in rows[1:6]))
    if year and not any(r["period"].startswith(str(year)) for r in series):
        lines.append(f"    ⚠️ 库中没有 {year} 年的数据，上面给的是可得的最近期次。")
    if res.get("note"):
        lines.append(f"    说明：{res['note']}")
    return lines


def _fmt_compare(call: dict) -> list[str]:
    res = call["result"]
    if not call["ok"]:
        return [f"- 对比失败：{res.get('message')}"]
    lines = [f"- 「{res['indicator']['name']}」横向对比（{res.get('report_type')}）："]
    for r in res.get("rows") or []:
        lines.append(f"    {r['rank']}. {r['name']}：{r['display']}"
                     f"（{r['period']}，来源：{_human_source(r['source'])}）")
    if res.get("missing"):
        lines.append("    缺数据：" + "、".join(
            f"{m['name']}({m['code']})" for m in res["missing"]))
    if res.get("note"):
        lines.append(f"    ⚠️ {res['note']}")
    return lines


def compose(question: str, companies: list[dict], ratios: list[str], inds: list[str],
            tool_calls: list[dict], year: int | None) -> str:
    """按模板拼答案。**不经过模型** —— 数值题要的是"数对且可溯源"，不是文采。"""
    header = "【数值分析】数据来自结构化财务库"
    lines = [header, ""]
    if not tool_calls:
        return ""

    ratios_done = [c for c in tool_calls if c["name"] == T_RATIO]
    compare_done = [c for c in tool_calls if c["name"] == T_COMPARE]
    ind_done = [c for c in tool_calls if c["name"] == T_INDICATOR]

    if ratios_done:
        lines.append(f"比率（{'、'.join(ratios)}）：")
        for c in ratios_done:
            lines.extend(_fmt_ratio(c))
        lines.append("")
    for c in compare_done:
        lines.extend(_fmt_compare(c))
        lines.append("")
    for c in ind_done:
        lines.extend(_fmt_indicator(c, year))
        lines.append("")

    bad = [c for c in tool_calls if not c["ok"]]
    if bad:
        lines.append("（以上有查询失败项，原因已随行标注："
                     + "；".join(f"{c['name']}: {c['error']}" for c in bad) + "）")
    lines.append(_scope_note(companies, ratios, inds))
    return "\n".join(lines).rstrip()


def _scope_note(companies: list[dict], ratios: list[str], inds: list[str]) -> str:
    who = "、".join(c["name"] for c in companies) or "（未识别到公司）"
    what = "、".join(ratios + inds) or "（未识别到指标）"
    return f"\n查询口径：公司={who}；指标={what}。以上数值不含任何推算，未出现在此处的数字请勿采信。"


# ==================== 子图节点 ====================

def analysis_question(state: QAState) -> QAState:
    """数值题的入口：抽实体 → 调工具 → 确定性组句。

    三条**不猜**的边界（都是刻意的）：
    - 认不出公司 → 不调工具，直接说明并列出可选公司（避免张冠李戴）；
    - 认不出指标 → 同上（路由层已用同一张别名表把关，走到这里说明别名表要更新）；
    - 多公司但期次不一致 → 照工具返回的 `periods_consistent=false` 如实转述，
      并**不把跨期次排名说成同口径排名**。
    """
    question = state.get("question", "") or ""
    matched = ((state.get("route") or {}).get("matched")) or {}
    notes = list(state.get("notes") or [])

    pool: list[dict] = []
    try:
        pool = list_companies()
    except Exception as e:                            # noqa: BLE001
        notes.append(f"公司库不可读：{type(e).__name__}: {e}")

    companies = detect_companies(question, pool=pool)
    if state.get("code"):
        forced = resolve_company(str(state["code"]), pool=pool) if pool else {"ok": False}
        companies = ([{"code": forced["code"], "name": forced["name"],
                       "industry": forced.get("industry", ""), "matched_by": "request"}]
                     if forced.get("ok") else companies)

    ratios, inds = resolve_names(question, matched)
    year = state.get("year") or detect_year(question)

    if not companies or not (ratios or inds):
        why = ("未能从问题中识别出库内公司" if not companies
               else "未能识别出库内指标名")
        listing = "、".join(f"{c['name']}({c['code']})" for c in pool[:12]) or "（公司库为空）"
        answer = (f"【数值分析】{why}，因此没有调用取数工具、也没有给出任何数字。\n\n"
                  f"- 库内可查公司：{listing}\n"
                  f"- 可查指标：{'、'.join(sorted(config.INDICATORS))}\n"
                  f"- 可查比率：{'、'.join(sorted(config.RATIOS))}\n\n"
                  f"请用标准公司名/代码与库内指标名重问一次（例如「贵州茅台 2024 年营业总收入」）。")
        return {
            "answer": answer, "refused": False, "refusal_reason": None,
            "degraded": False, "confidence": 0.55, "citations": [],
            "tool_calls": [], "evidence": {"kind": "analysis", "companies": [],
                                           "ratios": ratios, "indicators": inds},
            "notes": notes + [f"数值题未取数：{why}（不给数字比给错数字好）。"],
            "disclaimer": config.ANSWER_DISCLAIMER,
        }

    plan = plan_calls(companies, ratios, inds, year)
    calls = run_plan(plan)
    answer = compose(question, companies, ratios, inds, calls, year)

    got_data = any(c["ok"] for c in calls)
    if not got_data:
        notes.append("所有取数调用都失败或为空 —— 这是**结论**（该指标对该公司不适用/未入库），"
                     "不是系统故障；请按答案里的说明处理。")

    supported = sum(1 for c in calls if c["ok"])
    conf = 0.85 if got_data and supported == len(calls) and calls else (0.65 if got_data else 0.55)

    return {
        "answer": answer,
        "refused": False,
        "refusal_reason": None,
        "degraded": False,
        "confidence": conf,
        "citations": [],
        "tool_calls": [{k: v for k, v in c.items() if k != "result"} for c in calls],
        "tool_results": [c["result"] for c in calls],
        "evidence": {"kind": "analysis", "companies": companies,
                     "ratios": ratios, "indicators": inds, "year": year},
        "notes": notes + [f"数值题强制走工具层：共调用 {len(calls)} 次，"
                          f"成功 {supported} 次；所有数字均来自工具返回，"
                          f"无模型推算。"],
        "disclaimer": config.ANSWER_DISCLAIMER,
    }
