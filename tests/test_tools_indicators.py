"""指标查询工具测试（src/tools/indicators.py）。

重点覆盖三类**容易静默出错**的地方：
1. 公司归一 —— 简称包含匹配必须"唯一命中才接受"，命中多家要报歧义，
   否则"平安"会被随机解析成平安银行或中国平安，而调用方完全看不出来。
2. "查不到"的分级 —— 指标名不认识 / 公司不认识 / 认识但库中无数据，
   三者必须是三种不同返回；把第三种也说成"错误"会误导用户以为系统坏了。
3. 对比时各公司期次不一致必须显式告警 —— 否则就是拿 2025 年比 2024 年还叫"排名"。
"""
from __future__ import annotations

import pytest

from src.tools import indicators as ind


# ==================== 公司归一 ====================

def test_resolve_company_by_code_and_name(synth_db):
    assert ind.resolve_company("600519")["name"] == "贵州茅台"
    assert ind.resolve_company("贵州茅台")["code"] == "600519"


def test_resolve_company_unique_partial_name(synth_db):
    """唯一命中允许包含匹配，并标注 matched_by 让调用方知道这是模糊命中的。"""
    got = ind.resolve_company("茅台")
    assert got["ok"] and got["code"] == "600519"
    assert got.get("matched_by") == "partial_name"


def test_resolve_company_ambiguous_partial_name(synth_db):
    """"平安"同时命中中国平安与平安银行 → 必须报歧义并给候选，不能挑一个。"""
    got = ind.resolve_company("平安")
    assert got["ok"] is False
    assert got["error"] == "ambiguous_company"
    codes = {c["code"] for c in got["candidates"]}
    assert codes == {"601318", "000001"}


def test_resolve_company_unknown(synth_db):
    got = ind.resolve_company("不存在的公司")
    assert got["ok"] is False
    assert got["error"] == "unknown_company"
    assert {c["code"] for c in got["available_companies"]} == {
        "600519", "000858", "601318", "000001"}


def test_resolve_company_empty(synth_db):
    assert ind.resolve_company("  ")["error"] == "empty_company"


# ==================== 单指标序列 ====================

def test_get_indicator_series_is_desc_and_carries_source(synth_db):
    got = ind.get_financial_indicator("贵州茅台", "营业收入", periods=10)
    assert got["ok"] is True
    periods = [s["period"] for s in got["series"]]
    assert periods == ["2025-12-31", "2024-12-31", "2023-12-31"]     # 按期倒序
    latest = got["latest"]
    assert abs(latest["value"] - 1.10e11) < 1
    assert latest["display"] == "1,100.00 亿元"
    # 每个值都要能溯源到 具体报表.字段
    assert latest["source"] == "income.OPERATE_INCOME"
    assert got["indicator"]["expected_source"] == "income.OPERATE_INCOME"
    assert got["indicator"]["statement"] == "利润表"


def test_get_indicator_reports_actual_hit_source_not_preferred(synth_db):
    """保险股总资产实际命中 dc_balance，返回值必须如实说是 dc_balance。

    若这里返回配置里的首选源 `balance`，就是在说谎 —— F10 对保险公司整表为空，
    值实际来自数据中心报表。所以断言"实际命中的源"而不是"期望的源"。
    """
    got = ind.get_financial_indicator("中国平安", "总资产")
    assert got["series"][0]["source"] == "dc_balance.TOTAL_ASSETS"


def test_get_indicator_unknown_indicator_lists_known(synth_db):
    got = ind.get_financial_indicator("贵州茅台", "瞎写的指标")
    assert got["ok"] is False
    assert got["error"] == "unknown_indicator"
    assert "营业总收入" in got["known_indicators"]


def test_get_indicator_ambiguous_alias_gives_hint(synth_db):
    """「利润」是有口径歧义的说法 → 报错时要解释歧义在哪，而不是干巴巴"不认识"。"""
    got = ind.get_financial_indicator("贵州茅台", "利润")
    assert got["ok"] is False
    assert got["error"] == "unknown_indicator"
    assert "净利润与归母净利润" in got["message"]


def test_get_indicator_missing_data_is_ok_true_with_note(synth_db):
    """保险公司没有毛利率 → ok=true + 空序列 + note（这是结论，不是故障）。"""
    got = ind.get_financial_indicator("中国平安", "毛利率")
    assert got["ok"] is True
    assert got["series"] == []
    assert got["latest"] is None
    assert "不适用" in got["note"]


def test_get_indicator_unknown_company_propagates(synth_db):
    got = ind.get_financial_indicator("不存在的公司", "营业收入")
    assert got["ok"] is False and got["error"] == "unknown_company"


def test_get_indicator_report_type_alias(synth_db):
    """LLM 常写 "annual"，要能被归一成年报而不是查不到。"""
    assert ind.normalize_report_type("annual") == "年报"
    assert ind.normalize_report_type("年度") == "年报"
    assert ind.normalize_report_type(None) == "年报"
    got = ind.get_financial_indicator("贵州茅台", "营业收入", report_type="annual")
    assert got["report_type"] == "年报" and got["count"] == 3


def test_get_indicator_alias_resolves_to_canonical(synth_db):
    """「营收」是「营业总收入」的别名，要能解析且返回值里标明标准名。"""
    got = ind.get_financial_indicator("贵州茅台", "营收")
    assert got["ok"] is True
    assert got["indicator"]["name"] == "营业总收入"


# ==================== 多公司对比 ====================

def test_compare_ranks_desc(synth_db):
    got = ind.compare_companies(["贵州茅台", "五粮液", "中国平安"], "ROE")
    assert got["ok"] is True
    ranked = [(r["name"], r["value"]) for r in got["rows"]]
    assert ranked == [("五粮液", 20.0), ("贵州茅台", 17.5), ("中国平安", 13.8)]
    assert [r["rank"] for r in got["rows"]] == [1, 2, 3]
    assert got["periods_consistent"] is True


def test_compare_flags_inconsistent_periods(synth_db):
    """600519 已有 2025 期营业收入、000858 只有 2024 期 → 必须告警，不能当真排名。"""
    got = ind.compare_companies(["贵州茅台", "五粮液"], "营业收入")
    assert got["ok"] is True
    assert got["periods_consistent"] is False
    assert {r["period"] for r in got["rows"]} == {"2025-12-31", "2024-12-31"}
    assert "期次不一致" in got["note"]


def test_compare_specific_period_makes_it_consistent(synth_db):
    """显式指定期次后，期次必然一致 —— 这也是"要同口径对比就显式传期次"的用法。"""
    got = ind.compare_companies(["贵州茅台", "五粮液"], "营业收入",
                               period="2024-12-31")
    assert got["periods_consistent"] is True
    assert {r["period"] for r in got["rows"]} == {"2024-12-31"}


def test_compare_puts_company_without_data_into_missing(synth_db):
    got = ind.compare_companies(["贵州茅台", "中国平安"], "毛利率")
    assert got["ok"] is True
    assert [r["name"] for r in got["rows"]] == ["贵州茅台"]
    assert [m["name"] for m in got["missing"]] == ["中国平安"]
    assert "中国平安" in got["note"]


def test_compare_unresolvable_company_goes_to_errors(synth_db):
    got = ind.compare_companies(["贵州茅台", "五粮液", "不存在的公司"], "ROE")
    assert got["ok"] is True
    assert len(got["rows"]) == 2
    assert got["errors"][0]["input"] == "不存在的公司"
    assert got["errors"][0]["error"] == "unknown_company"


def test_compare_unknown_indicator(synth_db):
    got = ind.compare_companies(["贵州茅台", "五粮液"], "瞎写的指标")
    assert got["ok"] is False and got["error"] == "unknown_indicator"


def test_compare_requires_two_companies(synth_db):
    got = ind.compare_companies(["贵州茅台"], "ROE")
    assert got["ok"] is False and got["error"] == "need_at_least_two"


# ==================== 口径替代项（不适用时不说"没有"，而是给对应科目）====================

def test_missing_indicator_returns_counterpart_with_value(synth_db):
    """保险股问「营业成本」→ 空序列 + counterpart 带出「营业支出」的最新值与来源。

    这是对"用户看到 App 上有、我们却说没有"那类反馈的正确回应：
    保险业确实没有「营业成本」科目，但它有对应项「营业支出」，只回"没有"等于把问题推回去。
    注意断言的是**两个字段平级**（indicator 为空、counterpart 单独一个键），
    这样 LLM 转述时才有机会说清"这是替代口径"，不会把营业支出的值当成营业成本。
    """
    got = ind.get_financial_indicator("中国平安", "营业成本", periods=3)
    assert got["ok"] is True
    assert got["count"] == 0 and got["series"] == []
    assert got["latest"] is None

    cp = got["counterpart"]
    assert cp is not None
    assert "保险" in cp["why"]
    assert cp["indicator"]["name"] == "营业支出"
    assert cp["indicator"]["latest"]["value"] == pytest.approx(8.20e11)
    assert cp["indicator"]["latest"]["display"] == "8,200.00 亿元"
    assert cp["indicator"]["latest"]["source"] == "income.TOTAL_OPERATE_COST"
    assert "口径替代项" in got["note"]


def test_missing_indicator_without_counterpart_stays_plain(synth_db):
    """没有登记替代项的指标照旧只报"缺" —— 不硬凑一个近义词出来。"""
    got = ind.get_financial_indicator("中国平安", "净利润")
    assert got["count"] == 0
    assert got["counterpart"] is None


def test_present_indicator_has_no_counterpart_noise(synth_db):
    """指标正常有值时不该出现 counterpart 字段内容。"""
    got = ind.get_financial_indicator("贵州茅台", "营业成本")
    assert got["count"] >= 1
    assert got["counterpart"] is None


# ==================== 可用范围自述 ====================

def test_list_supported(synth_db):
    got = ind.list_supported()
    assert got["ok"] is True
    assert {c["code"] for c in got["companies"]} == {
        "600519", "000858", "601318", "000001"}
    assert "营业总收入" in got["indicators"]
    assert "营业支出" in got["indicators"]
    assert "毛利率" in got["ratios"]
    assert "毛利率(保险口径)" in got["ratios"]
    assert "年报" in got["report_types"]
