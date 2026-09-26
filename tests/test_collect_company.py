"""`collect_company` 的期间轴与缺失统计测试 —— 离线（打桩 fetch_source）。

这批用例锁的是**一个曾经真实发生过的误报**：
新浪补充源给到了主源范围之外更早的一期（601318 主源 2020–2025，新浪多给 2019），
若把"各源期次的并集"当期间轴，就会凭空多出一个只有新浪 3 个字段的残缺期，
其余指标在该期全部"缺失"，汇总时被误报成"全源缺失（数据源未提供）"——
把"某期没覆盖"和"压根没这个字段"混成了一件事。

规则：
- 期间轴由**主源**决定，补充源只在主源已确定的期上补字段；
- `missing`   = 每一期都取不到（数据源确实没这个字段）
- `partial`   = 只在部分期缺（覆盖度问题），且**不含** missing 里的指标（不重复计数）
- `source_errors` = 接口故障，与"数据不存在"分开
"""
from __future__ import annotations

import pytest

from src import config
from src.ingest import fetch_eastmoney as fe
from src.ingest import fetch_sina


@pytest.fixture
def stub_sources(monkeypatch):
    """打桩取数：按 source_key 返回构造好的行。"""
    rows: dict[str, list[dict]] = {}

    def _fetch(source_key, code, periods, report_type=None):    # noqa: ANN001
        return rows.get(source_key, [])

    monkeypatch.setattr(fe, "fetch_source", _fetch)
    fetch_sina.clear_cache()
    return rows


def _row(period: str, **fields) -> dict:
    return {"REPORT_DATE": period, **fields}


def test_period_axis_comes_from_primary_sources_not_union(stub_sources):
    """新浪多给的早期必须被丢掉 —— 它不该扩出期间轴。"""
    stub_sources["income"] = [
        _row("2024-12-31", OPERATE_INCOME=1.0e12, TOTAL_OPERATE_COST=8.2e11,
             TOTAL_OPERATE_INCOME=1.0e12, PARENT_NETPROFIT=1.2e11),
        _row("2023-12-31", OPERATE_INCOME=9.0e11, TOTAL_OPERATE_COST=7.4e11,
             TOTAL_OPERATE_INCOME=9.0e11, PARENT_NETPROFIT=1.0e11),
    ]
    stub_sources["sina_balance"] = [
        _row("2024-12-31", **{"归属于母公司的股东权益合计": 9.0e11}),
        _row("2023-12-31", **{"归属于母公司的股东权益合计": 8.5e11}),
        _row("2019-12-31", **{"归属于母公司的股东权益合计": 6.0e11}),   # ← 主源没有这一期
    ]

    got = fe.collect_company("601318", periods=6)
    assert got["periods"] == ["2024-12-31", "2023-12-31"]      # 2019 被丢掉
    assert "2019-12-31" not in got["by_period"]


def test_supplement_source_fills_field_missing_from_primary(stub_sources):
    """补缺的本意：主源拿不到的字段由补充源填，并**如实标注实际命中的源**。"""
    stub_sources["income"] = [
        _row("2024-12-31", OPERATE_INCOME=1.0e12, TOTAL_OPERATE_INCOME=1.0e12,
             TOTAL_OPERATE_COST=8.2e11, PARENT_NETPROFIT=1.2e11)]
    # 东财没有归母净资产 → 由新浪 fzb 补
    stub_sources["sina_balance"] = [
        _row("2024-12-31", **{"归属于母公司的股东权益合计": 9.0e11})]

    got = fe.collect_company("601318")
    rows = {r["indicator"]: r for r in got["by_period"]["2024-12-31"]}
    assert rows["归母净资产"]["value"] == pytest.approx(9.0e11)
    assert rows["归母净资产"]["source_table"] == "sina_balance"
    assert rows["归母净资产"]["source_field"] == "归属于母公司的股东权益合计"
    assert "sina_balance" in got["sources_used"]


def test_missing_vs_partial_are_separated_and_not_double_counted(stub_sources):
    """`missing`（每期都缺）与 `partial`（部分期缺）必须分开，且 partial 排掉 missing。

    场景：营收/成本只在 2024 期有，2023 期完全没有 → 它们属于 missing（每期没全给）。
    另造一个"只在 2023 期缺"的指标，才能同时覆盖 partial 分支。
    """
    stub_sources["income"] = [
        _row("2024-12-31", OPERATE_COST=4.0e10, OPERATE_INCOME=1.0e11),
        _row("2023-12-31", OPERATE_INCOME=9.0e10),      # 2023 缺 OPERATE_COST
    ]

    got = fe.collect_company("600519")
    # 营业成本在 2023 期缺、2024 期有 → 属于 partial，不属于 missing
    assert "营业成本" not in got["missing"]
    assert "营业成本" in got["partial"].get("2023-12-31", [])
    # 每期都没取到的（如毛利率/总资产…）进 missing，且不应再出现在 partial
    assert got["missing"], "应当有每期都缺的指标"
    for inds in got["partial"].values():
        assert not (set(inds) & set(got["missing"]))


def test_source_errors_are_surfaced_not_swallowed(stub_sources, monkeypatch):
    """接口故障要能出现在结果里，且与"数据不存在"分开报。"""
    fetch_sina._ERRORS["sina_balance"] = "RuntimeError: connection reset"   # noqa: SLF001
    stub_sources["income"] = [_row("2024-12-31", OPERATE_INCOME=1.0e11)]

    got = fe.collect_company("600519")
    assert got["source_errors"] == {"sina_balance": "RuntimeError: connection reset"}


def test_supplement_source_detection():
    """补充源按 api 判定，不靠硬编码名字。"""
    assert config.is_supplement_source("sina_balance") is True
    assert config.is_supplement_source("sina_income") is True
    assert config.is_supplement_source("income") is False
    assert config.is_supplement_source("dc_balance") is False
    assert config.is_supplement_source("不存在的源") is False


def test_data_sources_registry_shape():
    """源注册表：三个 api 各自登记齐全，且每个源都有 report_name 或 source。"""
    assert {v["api"] for v in config.DATA_SOURCES.values()} == \
        {"securities", "datacenter", "sina"}
    for key, spec in config.DATA_SOURCES.items():
        assert spec.get("report_name") or spec.get("source"), key
    # 需要服务端 REPORT_TYPE 过滤的源必须真有这个字段（数据中心那批没有）
    assert config.SOURCES_WITH_REPORT_TYPE <= set(config.DATA_SOURCES)
    assert not (config.SOURCES_WITH_REPORT_TYPE
                & {k for k, v in config.DATA_SOURCES.items() if v["api"] != "securities"})
