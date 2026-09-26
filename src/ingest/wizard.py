"""数据入库向导 —— 需求单模型、归一器，以及后续的解析/预览/入库编排。

## 这条通道为什么存在

结构化财务库的数据此前只能靠脚本预取（fetch_eastmoney / fetch_sina），
"想临时看一家新公司"就得开终端跑命令 —— 对不碰代码的使用者是断头路。
向导把它变成界面上的五步：自然语言 → 需求单 → 抓取预览 → 勾选 → 入库。

## 信任边界（设计 D2，本模块最重要的纪律）

LLM 只负责"听懂人话"，它的输出**一律不直接信任**：
认公司走 `resolve_company`、认指标/比率走 `config` 的既有别名归一 ——
与数值分析子图、路由用的是同一套解析，"路由认识、工具不认识"的漂移在这里不存在。
认不上的项**不丢弃、不报错**，进 `unresolved` 留给人在表单里改 ——
与数值子图「宁可拒答不猜」是同一条纪律。

## 与落库的关系（设计 D1）

本模块**不新写任何 SQL**：预览只调 `collect_company`（采集补缺，不落库），
确认入库才走 `save_company` 的既有 upsert 路径 —— 两函数在本仓已天然分离，
"只抓不写"的机器证明因此成为可能（见 tests/test_ingest_wizard.py 的行数不变断言）。
"""
from __future__ import annotations

import re
from uuid import uuid4

from pydantic import BaseModel, Field

from src import config, llm
from src.ingest.fetch_eastmoney import collect_company
from src.judge import parse_judge_json
from src.tools.indicators import list_companies, resolve_company

# 6 位纯数字 = A 股股票代码的原文形态（库外新公司的唯一抓取凭证）
_CODE_RE = re.compile(r"^\d{6}$")


class IngestPlan(BaseModel):
    """入库需求单：向导五步之间传递的唯一数据结构。

    companies 只存 `{code, name}`（渲染与勾选用），更多属性要用时
    再拿 code 去 resolve —— 不在需求单里冗余整份公司档案。
    """
    companies: list[dict] = Field(default_factory=list)
    indicators: list[str] = Field(default_factory=list)
    ratios: list[str] = Field(default_factory=list)
    periods: int | None = None          # 想要的年报期数；None = 让取数层用默认
    source: str = "eastmoney"           # 抓取渠道（D1：复用东财采集器）
    unresolved: list[str] = Field(default_factory=list)


def _as_str_list(value) -> list[str]:
    """宽容地把草稿里的列表字段变成非空 str 列表（数字代码等原样转 str）。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        value = [value]
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def _clamp_periods(years) -> int | None:
    """草稿的 years → 合法期数；乱输入一律 None（手填表单再给值），上限截断（D6）。"""
    if years is None or isinstance(years, bool):
        return None
    try:
        n = int(years)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return min(n, config.INGEST_MAX_PERIODS)


def _company_pool(pool: list[dict] | None) -> list[dict]:
    if pool is not None:
        return pool
    try:
        return list_companies()
    except Exception:                    # noqa: BLE001 —— 库不可用时不抛，全进 unresolved
        return []


def normalize_request(raw, pool: list[dict] | None = None) -> IngestPlan:
    """LLM 草稿 / 手填表单 → IngestPlan。

    归一**绝不发明**新公司/新指标（D2）：公司走 `resolve_company`（代码精确 >
    名称精确 > 名称包含，歧义不猜），指标/比率走 `config.resolve_indicator/resolve_ratio`。
    认不上的进 `unresolved` 保序保留原文；防呆上限（D6）超出即截断，
    超出部分不进 unresolved —— unresolved 的语义是"认不出"，不是"太多"。

    唯一例外是 6 位纯数字：向导的存在意义就是往库里加公司，新公司必然不在
    companies 表里、resolve 必然认不出 —— 6 位代码是抓取的原文凭证，直通收下
    （name 暂与代码相同，commit 补档案时即以代码为名）。库外公司**名字**没有这条路，
    仍然进 unresolved 等人改成代码 —— 宁可让人改，不可猜代码。
    """
    plan = IngestPlan()
    if not isinstance(raw, dict):
        return plan                      # None / 纯文本等乱输入 → 合法空单
    pool = _company_pool(pool)

    seen: set[str] = set()
    for name in _as_str_list(raw.get("companies")):
        res = resolve_company(name, pool=pool)
        if res.get("ok"):
            if res["code"] not in seen and len(plan.companies) < config.INGEST_MAX_COMPANIES:
                seen.add(res["code"])
                plan.companies.append({"code": res["code"], "name": res["name"]})
        elif _CODE_RE.match(name):
            # 库外新公司：companies 表里查不到不是错误，是向导要解决的问题本身
            if name not in seen and len(plan.companies) < config.INGEST_MAX_COMPANIES:
                seen.add(name)
                plan.companies.append({"code": name, "name": name})
        else:
            plan.unresolved.append(name)

    for name in _as_str_list(raw.get("indicators")):
        std = config.resolve_indicator(name)
        if std:
            if std not in plan.indicators:
                plan.indicators.append(std)
        else:
            plan.unresolved.append(name)
    plan.indicators = plan.indicators[:config.INGEST_MAX_INDICATORS]

    for name in _as_str_list(raw.get("ratios")):
        std = config.resolve_ratio(name)
        if std:
            if std not in plan.ratios:
                plan.ratios.append(std)
        else:
            plan.unresolved.append(name)
    plan.ratios = plan.ratios[:config.INGEST_MAX_INDICATORS]

    plan.periods = _clamp_periods(raw.get("years"))
    return plan


# ==================== 自然语言 → 需求单（Step 3） ====================

_SYSTEM_PLAN = (
    "你是金融数据入库助手。把用户的自然语言需求转成 JSON 需求单，**只输出 JSON**，格式为 "
    '{"companies": ["公司名或6位代码"], "indicators": ["科目名"],'
    ' "ratios": ["比率名"], "years": 最近几年年报的整数}。'
    "companies **优先填 6 位股票代码**（如 平安银行填 000001）—— 库里还没有的新公司只能靠代码抓取；"
    "不确定代码才填公司简称。indicators 填利润表/资产负债表/现金流量表科目"
    "（如 营业总收入、归母净利润、经营活动现金流净额）；ratios 填比率名（如 毛利率、ROE）。"
    "认不准就照抄用户原文，不要编造。不要输出解释文字，不要输出 Markdown 代码围栏。"
)


def _to_messages(system: str, user: str) -> list:
    from langchain_core.messages import HumanMessage, SystemMessage

    return [SystemMessage(content=system), HumanMessage(content=user)]


def _build_model():
    """与 `src/judge.py::_build_model` 同一套构造（同配置来源、同 JSON 模式）。"""
    from langchain_openai import ChatOpenAI

    cfg = llm.get_active_llm()
    return ChatOpenAI(
        model=cfg["model"], base_url=cfg["base_url"], api_key=cfg["api_key"],
        temperature=0.0,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def parse_request(text: str, llm_model=None, pool: list[dict] | None = None) -> dict:
    """自然语言 → `{"plan": IngestPlan, "note": str|None}`。

    LLM 只负责"听懂人话"出 JSON 草稿，认公司/认指标**必过 `normalize_request`**（D2）——
    它编造的公司进不了需求单，只会在 unresolved 里等人改。
    LLM 不可用（未配 Key / 构造失败 / 调用异常 / 剥不出 JSON）→
    空需求单 + note，**绝不抛异常** —— 手填表单路径照常可用。
    """
    empty = IngestPlan()
    if llm_model is None:
        ready, reason = llm.is_ready()
        if not ready:
            return {"plan": empty, "note": f"LLM 不可用（{reason}），请在下方表单手填"}
        try:
            llm_model = _build_model()
        except Exception as e:       # noqa: BLE001 —— 初始化失败不上抛
            return {"plan": empty,
                    "note": f"LLM 不可用（{type(e).__name__}），请在下方表单手填"}

    try:
        out = llm_model.invoke(_to_messages(_SYSTEM_PLAN, text or ""))
        content = out.content if isinstance(out.content, str) else str(out.content)
    except Exception as e:           # noqa: BLE001 —— 外部调用，失败必须收敛
        return {"plan": empty,
                "note": f"LLM 不可用（{type(e).__name__}: {e}），请在下方表单手填"}

    draft = parse_judge_json(content, metric="plan")   # 复用 judge 的围栏/寒暄剥壳
    if not isinstance(draft, dict):
        return {"plan": empty, "note": "LLM 不可用（输出无法解析为需求单），请在下方表单手填"}

    plan = normalize_request(draft, pool=pool)
    if plan.unresolved:
        note = f"{len(plan.unresolved)} 项未识别，请在表单中修改：" + "、".join(plan.unresolved[:3])
        return {"plan": plan, "note": note}
    return {"plan": plan, "note": None}


# ==================== dry-run 预览（Step 4：只抓不写） ====================

def _resolve_plan_companies(plan: IngestPlan, pool: list[dict]) -> list[dict]:
    """抓取前把需求单的公司归一到 `{code, name}`。

    plan 端点归一时已带 code 的原样收下；手填表单可能只有 name（用户改过字），
    走 `resolve_company` 兜底 —— 认不出的 code 置空，由调用方标 error，
    绝不猜一个相近的代码（金融场景选错公司比抓不到严重得多）。
    同 normalize 的例外：name 是 6 位纯数字（库外新公司的原文形态，必然
    resolve 不到）→ 直通当抓取代码，否则"往库里加新公司"在 preview 这步就死了。
    """
    out: list[dict] = []
    for c in plan.companies:
        code = (c.get("code") or "").strip()
        if _CODE_RE.match(code):
            out.append({"code": code, "name": c.get("name") or code})
            continue
        name = (c.get("name") or "").strip()
        res = resolve_company(name, pool=pool)
        if res.get("ok"):
            out.append({"code": res["code"], "name": res["name"]})
        elif _CODE_RE.match(name):
            out.append({"code": name, "name": name})
        else:
            out.append({"code": "", "name": name})
    return out


def preview(plan: IngestPlan) -> dict:
    """需求单 → 抓取预览。**只调 `collect_company`（采集补缺），绝不落库**。

    本仓 `collect_company`（采集）与 `save_company`（落库）天然分离 ——
    preview 只碰前者，"预览不改变库"由用例的行数不变断言守着（机器证明，
    不是靠注释自觉）。指标白名单：需求单勾了哪些指标就只预览哪些（空的 = 全部），
    "入库什么"与"需求单要什么"保持一致。
    单个公司抓取失败只把该公司行标 error（带原因），不整单失败（D6）。
    """
    out: list[dict] = []
    pool = _company_pool(None)
    for c in _resolve_plan_companies(plan, pool):
        code, name = c["code"], c["name"]
        if not code:
            out.append({"code": "", "name": name,
                        "error": "未识别的公司，请检查表单写法", "periods": [], "rows": []})
            continue
        try:
            res = collect_company(code, periods=plan.periods)
        except Exception as e:       # noqa: BLE001 —— 单公司失败不拖垮整单
            out.append({"code": code, "name": name, "error": f"{type(e).__name__}: {e}",
                        "periods": [], "rows": []})
            continue

        want = set(plan.indicators) | set(plan.ratios) or None
        rows: list[dict] = []
        for period in res.get("periods") or []:
            for r in (res.get("by_period") or {}).get(period) or []:
                if want is not None and r.get("indicator") not in want:
                    continue
                rows.append({
                    "period": r["period"], "indicator": r["indicator"],
                    "value": r["value"], "unit": r["unit"],
                    "source_table": r["source_table"], "source_field": r["source_field"],
                    # 补充源命中要在预览里显式标出：它的数来自新浪等第二源，口径可能与主源不同
                    "is_supplement": config.is_supplement_source(r.get("source_table", "")),
                })
        out.append({"code": code, "name": name, "error": None,
                    "periods": res.get("periods") or [], "rows": rows,
                    "missing": res.get("missing") or [], "partial": res.get("partial") or {}})
    return {"companies": out}


# ==================== commit 勾选写库 + 审计（Step 5） ====================

def commit(plan: IngestPlan, selected, actor: dict | None) -> dict:
    """勾选格入库。`selected` 是 `(code, period, indicator)` 三元组集合。

    写库**只走既有路径**（D1）：按 selected 过滤 collect 结果，交给
    `save_company` → `db.upsert_indicators_sql()`，本模块不新写一行指标 SQL。
    companies 表顺手补档案（复用 `db.upsert_companies_sql()`）—— 否则工具层
    resolve 不到新公司，"入库后立即可问"就是空话。

    审计（D3/D5）：batch id 用 uuid4，**没有批次表** —— batch 只活在
    audit_logs 的 detail_json 里，actor 取令牌的 sub。
    没勾任何格 → 直接拒绝（不写库、不写审计），不产生空批次。
    """
    from src import audit, db
    from src.ingest.fetch_eastmoney import save_company

    sel = {(s[0], s[1], s[2]) for s in (selected or [])}
    if not sel:
        return {"error": "未勾选任何数据，请先在预览里勾选要入库的格", "rows": 0}

    batch = uuid4().hex
    per_company: list[dict] = []
    total = 0
    pool = _company_pool(None)
    known_codes = {p.get("code") for p in pool}
    for c in _resolve_plan_companies(plan, pool):
        code = c["code"]
        if not code:
            per_company.append({"code": "", "name": c["name"], "rows": 0,
                                "error": "未识别的公司，未入库"})
            continue
        try:
            res = collect_company(code, periods=plan.periods)
        except Exception as e:       # noqa: BLE001 —— 单公司失败不拖垮整单
            per_company.append({"code": code, "rows": 0, "error": f"{type(e).__name__}: {e}"})
            continue
        for period in list(res.get("by_period") or {}):
            res["by_period"][period] = [
                r for r in res["by_period"][period]
                if (code, period, r.get("indicator")) in sel]
        if not any(res["by_period"].values()):
            per_company.append({"code": code, "rows": 0})
            continue
        stat = save_company(res)
        total += stat["rows"]
        per_company.append({"code": code, "rows": stat["rows"]})
        # 只给**库外**新公司补档案：companies 表是 resolve_company 的事实来源；
        # upsert 的 ON CONFLICT 会用 excluded 覆盖 market/industry/org_id，
        # 对既有公司恒传空串等于把档案抹掉（Step 10 代码审查发现①）。
        if code not in known_codes:
            with db.get_conn() as conn:
                conn.executemany(db.upsert_companies_sql(),
                                 [(code, c.get("name") or "", "", "", "")])

    actor = actor or {}
    audit.log("data_ingest", target=f"batch={batch}", actor=actor.get("sub"),
              detail={"batch": batch, "rows": total,
                      "plan": plan.model_dump(), "selected": sorted(sel),
                      "companies": per_company})
    return {"batch": batch, "rows": total, "companies": per_company}
