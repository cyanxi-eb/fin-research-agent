"""新浪补充源的取数规则测试 —— 全部离线（monkeypatch 掉 net.get_json）。

为什么这批用例很重要：新浪源是**唯一**能拿到保险股「归母净资产」的路，
它一旦静默出错（期次筛错、项目名变了），表现是"某项莫名缺失"，
而缺失在本项目里是**合法结论**，所以不会有任何异常冒出来 —— 只能靠用例锁住。

覆盖四个易错点：
1. 表代号必须是 `fzb`（传 `zcfz` 会返回 `data:null`，看着像"这只股票没有资产负债表"）；
2. 期次按 `date_description` 中文文本筛，**认不出的期次直接丢**（不猜类型）；
3. 项目名是中文，需与 config.INDICATORS 里的写法完全一致（多一个空格就是静默缺失）；
4. 单源失败不能拖垮整家公司，但必须进 `take_errors()` 暴露出来。
"""
from __future__ import annotations

import pytest

from src import config
from src.ingest import fetch_sina


def _payload(rows_by_date: dict[str, dict], desc_by_date: dict[str, str]) -> dict:
    """拼一个与新浪接口同形的返回体（只保留我们真正读的字段）。"""
    return {"result": {"data": {
        "report_date": [{"date_value": d, "date_description": t}
                        for d, t in desc_by_date.items()],
        "report_list": {d: {"data": [{"item_title": k, "item_value": v}
                                     for k, v in items.items()]}
                        for d, items in rows_by_date.items()},
    }}}


@pytest.fixture
def fake_sina(monkeypatch):
    """替换 net.get_json，记录调用参数，返回构造好的新浪响应。"""
    calls: list[dict] = []
    box: dict = {}

    def _get_json(url, params=None, referer=None):      # noqa: ANN001
        calls.append({"url": url, "params": params, "referer": referer})
        return box.get("resp") or _payload({}, {})

    monkeypatch.setattr(fetch_sina.net, "get_json", _get_json)
    fetch_sina.clear_cache()
    return {"calls": calls, "box": box}


# ==================== 表代号与请求参数 ====================

def test_balance_uses_fzb_table_code(fake_sina):
    """资产负债表必须传 `source=fzb`。

    `zcfz` 是很多人会先猜的代号，它返回 HTTP 200 + `{"status":{"code":0},"data":null}`
    —— 有状态码、没数据，看着像"这只股票没有资产负债表"。这里把正确代号钉死。
    """
    assert config.DATA_SOURCES["sina_balance"]["source"] == "fzb"
    assert config.DATA_SOURCES["sina_income"]["source"] == "lrb"

    fetch_sina.fetch_source("sina_balance", "601318", periods=6, report_type="年报")
    p = fake_sina["calls"][0]["params"]
    assert p["source"] == "fzb"
    assert p["paperCode"] == "sh601318"
    assert fake_sina["calls"][0]["referer"] == config.SINA_REFERER


def test_paper_code_suffixes_match_market():
    """三个市场的 paperCode 前缀：沪 sh / 深 sz / 北 bj。"""
    assert fetch_sina.to_paper_code("600519") == "sh600519"
    assert fetch_sina.to_paper_code("900901") == "sh900901"
    assert fetch_sina.to_paper_code("000858") == "sz000858"
    assert fetch_sina.to_paper_code("300750") == "sz300750"
    assert fetch_sina.to_paper_code("830799") == "bj830799"


def test_fetch_source_rejects_non_sina_key(fake_sina):
    """拿东财的 source_key 调新浪模块要直接报错，而不是偷偷发一个错的请求。"""
    with pytest.raises(ValueError, match="不是新浪源"):
        fetch_sina.fetch_source("income", "600519", periods=6)


# ==================== 期次筛选 ====================

def test_filters_by_period_description_not_by_date(fake_sina):
    """按 `date_description` 文本筛报告期类型，中报不能被混进年报序列。

    为什么不按"日期是 12-31"筛：季报/中报的期次说明文本是可靠的（"2025半年报"），
    而日期规则会把各种非年报期也放进来，序列就被污染了。
    """
    box = fake_sina["box"]
    box["resp"] = _payload(
        {"20241231": {"归属于母公司的股东权益合计": 928600000000},
         "20240630": {"归属于母公司的股东权益合计": 900000000000},
         "20231231": {"归属于母公司的股东权益合计": 899011000000}},
        {"20241231": "2024年报", "20240630": "2024半年报", "20231231": "2023年报"})

    got = fetch_sina.fetch_source("sina_balance", "601318", periods=6, report_type="年报")
    assert [r["REPORT_DATE"] for r in got] == ["2024-12-31", "2023-12-31"]

    # 切到中报，只应剩 2024-06-30
    fetch_sina.clear_cache()
    mid = fetch_sina.fetch_source("sina_balance", "601318", periods=6, report_type="中报")
    assert [r["REPORT_DATE"] for r in mid] == ["2024-06-30"]


def test_unknown_period_description_is_dropped_not_guessed(fake_sina):
    """期次说明认不出来的期次**丢掉**，不猜类型 —— 猜错会把中报当成年报入库。"""
    fake_sina["box"]["resp"] = _payload(
        {"20241231": {"所有者权益合计": 1.0},
         "20250331": {"所有者权益合计": 2.0}},
        {"20241231": "2024年报", "20250331": "看不懂的期次说明"})

    got = fetch_sina.fetch_source("sina_balance", "601318", periods=6, report_type="年报")
    assert [r["REPORT_DATE"] for r in got] == ["2024-12-31"]


def test_report_type_of_description_maps_and_rejects():
    """期次说明 → 标准类型；认不出返回 None（调用方负责丢掉）。"""
    assert config.report_type_of_description("2024年报") == "年报"
    assert config.report_type_of_description("2025半年报") == "中报"
    assert config.report_type_of_description("2025一季报") == "一季报"
    assert config.report_type_of_description("2025三季报") == "三季报"
    assert config.report_type_of_description("2025第1季度") is None
    assert config.report_type_of_description("") is None


def test_normalize_report_type_aliases():
    """LLM 常给 annual/年度/H1 这类写法，必须归一；认不出退回年报。"""
    assert config.normalize_report_type("annual") == "年报"
    assert config.normalize_report_type("年度") == "年报"
    assert config.normalize_report_type(None) == "年报"
    assert config.normalize_report_type("H1") == "中报"
    assert config.normalize_report_type("半年报") == "中报"
    assert config.normalize_report_type("乱七八糟") == "年报"


def test_iso_date_guard():
    assert fetch_sina._iso_date("20241231") == "2024-12-31"
    assert fetch_sina._iso_date("2024-12-31") is None      # 非 8 位数字 → 不猜
    assert fetch_sina._iso_date("") is None


# ==================== 项目名与值 ====================

def test_non_numeric_items_are_dropped(fake_sina):
    """非数值项目（带说明文字的行）不进数值表，避免下游 float() 炸掉。"""
    fake_sina["box"]["resp"] = _payload(
        {"20241231": {"所有者权益合计": "13,047.12亿", "归属于母公司的股东权益合计": 928600000000}},
        {"20241231": "2024年报"})

    got = fetch_sina.fetch_source("sina_balance", "601318", periods=6, report_type="年报")
    row = got[0]
    assert row["归属于母公司的股东权益合计"] == 928600000000
    assert "所有者权益合计" not in row       # 文本值被丢掉，而不是留成字符串
    assert row["_PERIOD_DESC"] == "2024年报"


def test_chinese_field_names_are_used_directly_as_indicator_sources():
    """口径表里新浪的字段名 = 报表中文项目名，逐字对得上（多一个空格就是静默缺失）。

    这是"取数层不需要行业分支"的前提：不管是东财的英文大写下划线还是新浪的中文项目名，
    上层统一用 `raw.get(field)` 取值，所以口径表必须写成报表里的原名。
    """
    sina_fields = {field for meta in config.INDICATORS.values()
                   for key, field in meta["sources"] if key.startswith("sina_")}
    assert sina_fields == {"归属于母公司的股东权益合计", "所有者权益合计",
                           "营业成本", "营业总成本", "营业支出"}


# ==================== 失败暴露 ====================

def test_source_failure_is_recorded_and_does_not_raise(monkeypatch):
    """单源报错 → 返回空列表（不拖垮整家公司），但错误必须进 take_errors()。

    静默吞掉是这类补充源最危险的失败模式：主链路照常跑完，
    只是保险股悄悄少了归母净资产，而"少一项"在本项目里是合法结论，没人会发现。
    """
    def _boom(url, params=None, referer=None):          # noqa: ANN001
        raise RuntimeError("connection reset")

    monkeypatch.setattr(fetch_sina.net, "get_json", _boom)
    fetch_sina.clear_cache()

    assert fetch_sina.fetch_source("sina_balance", "601318", periods=6) == []
    errs = fetch_sina.take_errors()
    assert "sina_balance" in errs
    assert "connection reset" in errs["sina_balance"]
    assert fetch_sina.take_errors() == {}               # 取走即清空，不会重复报


def test_cache_avoids_duplicate_requests(fake_sina):
    """同一个 (源, 代码, 期数) 只请求一次 —— 多指标共用一份源数据。"""
    fetch_sina.fetch_source("sina_income", "601318", periods=6, report_type="年报")
    fetch_sina.fetch_source("sina_income", "601318", periods=6, report_type="年报")
    assert len(fake_sina["calls"]) == 1
