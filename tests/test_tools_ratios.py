"""比率计算工具测试（src/tools/ratios.py）。

这组用例的核心不是"能不能算出数"，而是**口径与选期是否可核对**：
- 分母到底用的「营业收入」还是「营业总收入」——两者能差 1~2 个百分点，是真实事故源；
- 缺分项时是"报错"还是"当 0 算"——后者会算出一个看着正常的错值（静默错误）；
- 默认取"最新一期"还是"分项最齐的一期"——新出年报常常只补了一半分项；
- 派生值与官方值的差异要**如实分级**（一致 / 口径差异 / 需复核），不能一概说"对"或"错"。
"""
from __future__ import annotations

import pytest

from src.tools import ratios as rt


def _terms_of(bucket: dict) -> list[str]:
    return [t["indicator"] for t in bucket["terms"]]


# ==================== 口径：分母用的是哪个科目 ====================

def test_gross_margin_uses_operate_income_not_total_operate_income(synth_db):
    """毛利率分母必须是「营业收入」(1.0e11)，不能用「营业总收入」(1.02e11)。

    若误用营业总收入，结果会是 (1.0e11−4.0e10)/1.02e11 = 58.82%，与官方 60.00% 对不上。
    这条断言就是钉死这个口径。
    """
    got = rt.calc_financial_ratio("贵州茅台", "毛利率")
    assert got["ok"] is True
    assert _terms_of(got["denominator"]) == ["营业收入"]
    assert abs(got["denominator"]["value"] - 1.00e11) < 1
    assert "营业总收入" not in _terms_of(got["denominator"])
    assert got["ratio"]["formula"].startswith("(营业收入 − 营业成本)")


def test_gross_margin_value_and_cross_check(synth_db):
    got = rt.calc_financial_ratio("贵州茅台", "毛利率")
    assert abs(got["value"] - 60.0) < 1e-6
    assert got["display"] == "60.00%"
    # 分子 = 营业收入 − 营业成本，两个分项都要列出且带符号与来源
    assert _terms_of(got["numerator"]) == ["营业收入", "营业成本"]
    signs = {t["indicator"]: t["sign"] for t in got["numerator"]["terms"]}
    assert signs == {"营业收入": "+", "营业成本": "−"}
    assert all(t["source"] for t in got["numerator"]["terms"])
    # 派生值与官方完全同口径 → 判"一致"
    assert got["official"]["value"] == pytest.approx(60.0)
    assert got["official"]["source"] == "main.XSMLL"
    assert got["cross_check"]["verdict"] == "一致"


def test_net_margin_uses_total_operate_income(synth_db):
    """净利率分母是「营业总收入」（与官方 XSJLL 同口径），与毛利率分母**故意不同**。"""
    got = rt.calc_financial_ratio("贵州茅台", "净利率")
    assert _terms_of(got["denominator"]) == ["营业总收入"]
    assert got["value"] == pytest.approx(2.10e10 / 1.02e11 * 100, rel=1e-9)
    assert got["cross_check"]["verdict"] == "一致"


# ==================== 选期：分项最齐 vs 最新 ====================

def test_default_period_skips_latest_when_components_missing(synth_db):
    """2025 期只有营业收入 → 算毛利率必须回退到 2024 期，并说明为什么不用最新期。"""
    got = rt.calc_financial_ratio("贵州茅台", "毛利率")
    assert got["period"] == "2024-12-31"
    assert got["period_note"] is not None
    assert "2025-12-31" in got["period_note"] and "营业成本" in got["period_note"]


def test_period_note_absent_when_latest_is_complete(synth_db):
    """ROE 的分项在 2024 期就齐、2025 期没有该指标 → 不产生选期说明。"""
    got = rt.calc_financial_ratio("贵州茅台", "ROE")
    assert got["period"] == "2024-12-31"
    assert got["period_note"] is None


def test_explicit_period_is_respected(synth_db):
    got = rt.calc_financial_ratio("贵州茅台", "毛利率", period="2023-12-31")
    assert got["period"] == "2023-12-31"
    assert got["period_note"] is None
    assert got["value"] == pytest.approx(60.0)


def test_explicit_period_with_missing_component_reports_insufficient(synth_db):
    """显式点名 2025 期但该期缺营业成本 → 报缺项，而不是偷偷改用别的期去算。"""
    got = rt.calc_financial_ratio("贵州茅台", "毛利率", period="2025-12-31")
    assert got["ok"] is False
    assert got["error"] == "insufficient_components"
    assert got["missing_indicators"] == ["营业成本"]
    assert got["period"] == "2025-12-31"


def test_period_without_any_data(synth_db):
    got = rt.calc_financial_ratio("贵州茅台", "毛利率", period="2019-12-31")
    assert got["ok"] is False
    assert got["error"] == "no_data"


# ==================== 交叉对账分级 ====================

def test_roe_period_end_vs_weighted_is_flagged_not_hidden(synth_db):
    """ROE 期末口径 20.00% vs 官方加权 17.50%，差 2.5pp → 要如实标"差异较大"。

    这不是 bug：期末口径与加权平均口径本就会差（分红/增发使期中净资产变动）。
    正确做法是把差异与 note 一起交出去，而不是把 note 藏在代码里、只给一个数。
    """
    got = rt.calc_financial_ratio("贵州茅台", "ROE")
    assert got["value"] == pytest.approx(20.0)
    assert got["official"]["value"] == pytest.approx(17.5)
    assert got["cross_check"]["delta"] == pytest.approx(2.5)
    assert got["cross_check"]["verdict"] == "差异较大，需人工复核口径或数据期次"
    assert "加权平均" in got["ratio"]["note"]


def test_debt_ratio_matches_official(synth_db):
    got = rt.calc_financial_ratio("中国平安", "资产负债率")
    assert got["value"] == pytest.approx(1.10e13 / 1.20e13 * 100, rel=1e-9)
    assert got["cross_check"]["verdict"] == "一致"


def test_ratio_without_official_has_no_cross_check(synth_db):
    """派生比率（经营现金流净利润比）没有官方口径 → official/cross_check 均为 None，不编造。"""
    got = rt.calc_financial_ratio("贵州茅台", "经营现金流净利润比")
    assert got["ok"] is True
    assert got["value"] == pytest.approx(1.5)
    assert got["display"] == "1.50 倍"
    assert got["official"] is None
    assert got["cross_check"] is None


# ==================== 缺分项：报错而不是当 0 ====================

def test_missing_component_fails_loudly_instead_of_using_zero(synth_db):
    """分项一个都没有 → no_data（不是 insufficient_components，也不是 0）。

    用平安银行覆盖这条分支：合成库里它只有归母净利润，没有营业收入/营业成本。
    关键是**不能把缺失当 0**——那样毛利率会算出 100%，是个看着正常的错值。
    """
    got = rt.calc_financial_ratio("平安银行", "毛利率")
    assert got["ok"] is False
    assert got["error"] == "no_data"
    # needed_indicators 由 config.ratio_meta 归一为**排序后**的列表（按码点，不是书写顺序）
    assert set(got["needed_indicators"]) == {"营业收入", "营业成本"}
    assert "可能本就不适用" in got["message"]


def test_partial_missing_component_is_insufficient_not_no_data(synth_db):
    """部分缺失（有营业收入、缺营业成本）→ insufficient_components，与"全无"区分开。

    场景：新出年报只补了营业收入、还没补营业成本；显式点名该期就会走到这条分支。
    """
    got = rt.calc_financial_ratio("贵州茅台", "毛利率", period="2025-12-31")
    assert got["ok"] is False
    assert got["error"] == "insufficient_components"
    assert got["missing_indicators"] == ["营业成本"]


def test_insurance_gross_margin_offers_counterpart_caliber(synth_db):
    """保险股算毛利率 → 失败，但**必须带出替代口径**，不能只回"没有"。

    这是本项目对待"口径不存在"的立场：直接回一个数是在造假（那不是毛利率），
    只说"没有"是把问题推回给用户。正解是给出替代口径的名称、公式与数值。
    """
    got = rt.calc_financial_ratio("中国平安", "毛利率")
    assert got["ok"] is False
    assert got["missing_indicators"] == ["营业成本"]
    alts = got["alternatives"]
    assert len(alts) == 1
    alt = alts[0]
    assert alt["ratio"] == "毛利率(保险口径)"
    assert alt["ok"] is True
    assert alt["value"] == pytest.approx(18.0)          # (1.00e12 − 8.20e11)/1.00e12
    assert "营业支出" in alt["formula"]
    assert "不可横向比较" in alt["note"]


def test_insurance_caliber_computes_from_expense(synth_db):
    """「毛利率(保险口径)」用「营业支出」当成本项，分子分母逐项可核对。"""
    got = rt.calc_financial_ratio("中国平安", "保险毛利率")   # 走别名解析
    assert got["ok"] is True
    assert got["display"] == "18.00%"
    assert got["official"] is None          # 东财/同花顺都没有官方口径 → 不得编造
    assert got["cross_check"] is None
    terms = {t["indicator"]: t for t in got["numerator"]["terms"]}
    assert terms["营业支出"]["sign"] == "−"
    assert terms["营业支出"]["source"] == "income.TOTAL_OPERATE_COST"


def test_manufacturing_gross_margin_has_no_alternatives(synth_db):
    """制造业算毛利率正常出值 → 不应该冒出 alternatives（那是给失败路径的引导）。"""
    got = rt.calc_financial_ratio("贵州茅台", "毛利率", period="2024-12-31")
    assert got["ok"] is True
    assert got["value"] == pytest.approx(60.0)
    assert got.get("alternatives") is None


def test_insurance_roe_uses_sina_equity_and_flags_caliber_gap(synth_db):
    """保险股 ROE 能算出来 —— 因为归母净资产来自新浪源（东财全系都没有）。

    这条用例锁住的是"多源不只是容错、而是补缺"这一事实：
    若有人把「归母净资产」的 sources 改回只用东财，这里就会因缺分母而失败。
    同时官方 ROEJQ 是加权口径，派生值是期末口径，差 0.4667pp 应判为"口径差异"而非错误。
    """
    got = rt.calc_financial_ratio("中国平安", "ROE")
    assert got["ok"] is True
    assert got["display"] == "13.33%"
    assert got["denominator"]["terms"][0]["source"] == \
        "sina_balance.归属于母公司的股东权益合计"
    assert got["official"]["display"] == "13.80%"
    assert got["cross_check"]["verdict"].startswith("口径差异")


def test_insurance_roe_reports_missing_denominator(synth_db):
    """有分子没分母 → 明确报"缺归母净资产"，不动用 0。"""
    got = rt.calc_financial_ratio("平安银行", "ROE")
    assert got["ok"] is False
    assert got["error"] == "insufficient_components"
    assert got["missing_indicators"] == ["归母净资产"]


# ==================== 归一与错误路径 ====================

def test_ratio_alias_and_english_name(synth_db):
    assert rt.calc_financial_ratio("贵州茅台", "销售毛利率")["ok"] is True
    assert rt.calc_financial_ratio("贵州茅台", "GM")["ok"] is True
    assert rt.calc_financial_ratio("贵州茅台", "roe")["ok"] is True


def test_unknown_ratio(synth_db):
    got = rt.calc_financial_ratio("贵州茅台", "瞎写的比率")
    assert got["ok"] is False
    assert got["error"] == "unknown_ratio"
    assert "毛利率" in got["known_ratios"]


def test_unknown_company_propagates(synth_db):
    got = rt.calc_financial_ratio("不存在的公司", "毛利率")
    assert got["ok"] is False and got["error"] == "unknown_company"


def test_partial_missing_component_reports_which_one(synth_db):
    """000858 有归母净利润、没有经营活动现金流净额 → 报"缺哪一项"，而不是笼统说没数据。"""
    got = rt.calc_financial_ratio("五粮液", "经营现金流净利润比")
    assert got["ok"] is False
    assert got["error"] == "insufficient_components"
    assert got["missing_indicators"] == ["经营活动现金流净额"]
    assert got["needed_indicators"] == ["归母净利润", "经营活动现金流净额"]


def test_no_data_when_company_has_none_of_needed(synth_db):
    """000001 平安银行一行指标都没灌 → 是 no_data（与"缺分项"区分开）。"""
    got = rt.calc_financial_ratio("平安银行", "毛利率")
    assert got["ok"] is False
    assert got["error"] == "no_data"


# ==================== 输出可序列化 ====================

def test_result_is_json_serializable(synth_db):
    """工具返回值要能直接落审计库/吐给前端，不能带 Row、Decimal 之类。"""
    import json

    for ratio in ("毛利率", "净利率", "资产负债率", "ROE", "经营现金流净利润比"):
        got = rt.calc_financial_ratio("贵州茅台", ratio)
        json.dumps(got, ensure_ascii=False)
