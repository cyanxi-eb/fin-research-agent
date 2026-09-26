"""多公司对比契约用例（`src/compare.py` + `/api/compare`，跑在 `synth_db` 上，不联网）。

四条要在实现前先钉死的事：

1. **返回结构固定**：前端对比表与趋势图直接吃 `compare()` 的返回，
   字段一漂就是"前端静默画错"，所以 `ok/indicator/unit/rows/periods_consistent/chart/note`
   以及每行 `code/name/period/value/unit/source` 都要被用例锁住。
2. **跨期次比较必须显式告警**：工具给的是"各自最新一期"，各家公司年报进度不同，
   静默按最新一期排名 = 拿不同年份比大小（金融数据事故）。故 `periods_consistent=False`
   时 `note` 必须非空，并写明**每家实际是哪一期**。
3. **趋势图必须真的多期**：`chart.series[i].points` 要覆盖库里全部可用期次（升序），
   否则"趋势图"退化成孤点 = 假的功能；`chart.periods` 为全局有序期次；显式 `period=`
   时退化为单点。
4. **参数错走固定 error 码**（不认识指标 / 公司不足 2 家），不是抛异常、也不是 500。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import compare
from src.server import app

# 契约里固定的顶层键（错误场景另带 error，不在此列）
FIXED_KEYS = {"ok", "indicator", "unit", "rows", "periods_consistent", "chart", "note"}
ROW_KEYS = {"code", "name", "period", "value", "unit", "source"}


# ==================== 参数错：固定 error 码 ====================

def test_unknown_indicator_is_structured_error(synth_db):
    got = compare.compare("不存在的指标", ["600519", "000858"])
    assert got["ok"] is False
    assert got["error"] == "unknown_indicator"
    assert got["rows"] == []
    assert got["chart"] == {"series": [], "periods": []}
    assert FIXED_KEYS <= set(got)


def test_need_at_least_two_companies(synth_db):
    got = compare.compare("营业总收入", ["600519"])
    assert got["ok"] is False
    assert got["error"] == "need_at_least_two_companies"
    assert got["rows"] == []
    assert got["chart"] == {"series": [], "periods": []}


# ==================== 多期趋势：必须真的取到多期，而不是"最新一期孤点" ====================

def _add_annual(db_path, code, indicator, periods):
    """给合成库补若干期年报数据（只动临时库，不碰真实库）。"""
    from src import db

    with db.get_conn(db_path=db_path) as conn:
        conn.executemany(
            db.upsert_indicators_sql(),
            [(code, p, "年报", indicator, v, "元", "income", "TOTAL_OPERATE_INCOME")
             for p, v in periods])


@pytest.fixture
def synth_db_multiyear(synth_db):
    """两家公司都凑成 2021–2024 四期营业总收入，专测 chart 的多期序列。

    注意合成库里 000858 **没有**营业总收入（只有营业收入），所以要把它补全，
    否则两家最新期不同、期次不一致，测不出"多期"这件事。
    """
    _add_annual(synth_db, "600519", "营业总收入",
                [("2021-12-31", 9.00e10), ("2022-12-31", 9.50e10), ("2023-12-31", 9.80e10)])
    _add_annual(synth_db, "000858", "营业总收入",
                [("2021-12-31", 6.00e10), ("2022-12-31", 6.50e10),
                 ("2023-12-31", 7.00e10), ("2024-12-31", 8.00e10)])
    return synth_db


def test_chart_points_cover_all_periods_ascending(synth_db_multiyear):
    """period=None 时，每条序列要覆盖库里全部可用期次，且按期次升序。"""
    got = compare.compare("营业总收入", ["600519", "000858"])

    assert got["ok"] is True
    assert got["periods_consistent"] is True
    chart = got["chart"]

    # 全局期次 = 各系列期次的升序并集
    assert chart["periods"] == ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31"]
    assert len(chart["series"]) == 2
    for s in chart["series"]:
        periods = [p["period"] for p in s["points"]]
        assert len(periods) >= 3, f"趋势序列退化成孤点：{s['code']} → {periods}"
        assert periods == sorted(periods), "points 必须按期次升序"
        assert periods == chart["periods"], "每条序列应覆盖同一批全期次"


def test_explicit_period_degenerates_to_single_point(synth_db_multiyear):
    """显式 period 时退化为单点，且 chart.periods == [period]。"""
    got = compare.compare("营业总收入", ["600519", "000858"], period="2023-12-31")

    assert got["periods_consistent"] is True
    assert got["chart"]["periods"] == ["2023-12-31"]
    for s in got["chart"]["series"]:
        assert s["points"] == [{"period": "2023-12-31", "value": s["points"][0]["value"]}]
        assert len(s["points"]) == 1


# ==================== 正常：结构 / 排序 / chart ====================

def test_rows_and_chart_contract(synth_db):
    """两家同期（2024）的营业总收入：结构、单位、排序、chart.series 都要对。"""
    got = compare.compare("营业总收入", ["600519", "601318"])

    assert FIXED_KEYS <= set(got)
    assert got["ok"] is True
    assert got["indicator"] == "营业总收入"
    assert got["unit"] == "元"
    assert got["periods_consistent"] is True
    assert len(got["rows"]) == 2

    for row in got["rows"]:
        assert ROW_KEYS <= set(row), f"行缺字段：{ROW_KEYS - set(row)}"
        assert row["unit"] == "元"
        assert row["source"], "每个数都要能回溯到 表.字段"

    # 排名按数值降序：601318（1.00e12）> 600519（1.02e11）
    assert [r["code"] for r in got["rows"]] == ["601318", "600519"]

    series = got["chart"]["series"]
    assert [s["code"] for s in series] == ["601318", "600519"]
    for s in series:
        assert {"code", "name", "points"} <= set(s)
        assert len(s["points"]) == 1
        assert set(s["points"][0]) == {"period", "value"}
        assert s["points"][0]["period"] == "2024-12-31"
    # 两家该指标在合成库里都只有 2024 一期 → 全局期次就是它
    assert got["chart"]["periods"] == ["2024-12-31"]


# ==================== 跨期次：必须告警并写明各家期次 ====================

def test_inconsistent_periods_are_flagged_in_note(synth_db):
    """600519（营业收入有 2025 年报）vs 000858（只到 2024）→ 期次不一致。"""
    got = compare.compare("营业收入", ["600519", "000858"])

    assert got["ok"] is True
    assert got["periods_consistent"] is False
    assert got["note"], "跨期次比较必须给出说明，不能静默排名"

    # note 必须说清「各家实际是哪一期」，而不是只给一个期次集合
    for token in ("600519", "000858", "2025-12-31", "2024-12-31"):
        assert token in got["note"], f"note 漏了 {token}：{got['note']}"

    periods = {r["code"]: r["period"] for r in got["rows"]}
    assert periods == {"600519": "2025-12-31", "000858": "2024-12-31"}


def test_explicit_period_aligns_companies(synth_db):
    """指定期后两家同口径（2024），期次一致、无需告警。"""
    got = compare.compare("营业收入", ["600519", "000858"], period="2024-12-31")

    assert got["ok"] is True
    assert got["periods_consistent"] is True
    assert [r["period"] for r in got["rows"]] == ["2024-12-31", "2024-12-31"]
    # 1.00e11（600519）> 8.00e10（000858）
    assert [r["code"] for r in got["rows"]] == ["600519", "000858"]
    assert not got["note"]
    # 显式 period → 单点 + 全局期次就是该期
    assert got["chart"]["periods"] == ["2024-12-31"]
    for s in got["chart"]["series"]:
        assert len(s["points"]) == 1
        assert s["points"][0]["period"] == "2024-12-31"


# ==================== HTTP 面：GET / POST 同一实现 ====================

@pytest.fixture
def client(synth_db):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_multiyear(synth_db_multiyear):
    with TestClient(app) as c:
        yield c


def test_compare_endpoint_get_and_post_agree(client):
    params = {"indicator": "营业总收入", "codes": "600519,601318", "period": "2024-12-31"}
    got = client.get("/api/compare", params=params)
    assert got.status_code == 200
    body = got.json()
    assert body["ok"] is True and len(body["rows"]) == 2
    assert body["chart"]["series"]
    assert body["chart"]["periods"] == ["2024-12-31"]

    posted = client.post("/api/compare", json={"indicator": "营业总收入",
                                               "codes": ["600519", "601318"],
                                               "period": "2024-12-31"})
    assert posted.status_code == 200
    # GET 与 POST 必须同形同值，否则前端行为会随提交方式漂移
    assert posted.json()["rows"] == body["rows"]


def test_compare_endpoint_chart_has_multiple_periods(client_multiyear):
    """HTTP 面同样要拿到多期：证明服务层没有把趋势退化成孤点。"""
    body = client_multiyear.get("/api/compare", params={
        "indicator": "营业总收入", "codes": "600519,000858"}).json()

    assert body["ok"] is True
    assert len(body["chart"]["periods"]) >= 3
    for s in body["chart"]["series"]:
        assert len(s["points"]) == len(body["chart"]["periods"])


def test_compare_endpoint_bad_params_are_400(client):
    assert client.get("/api/compare", params={
        "indicator": "不存在的指标", "codes": "600519,601318"}).status_code == 400
    assert client.get("/api/compare", params={
        "indicator": "营业总收入", "codes": "600519"}).status_code == 400