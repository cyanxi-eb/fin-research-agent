"""种子导入（scripts/load_seed.py）测试。

只测一件核心性质：**在后端中立的 business.json 上，load_seed 灌进 SQLite 后
四张表行数与种子一致，且重复执行行数不变**。这是容器"每次重启都跑一遍 load_seed"
能否安全的前提 —— 幂等一破，重启就会把数据灌成两倍。

刻意不测真实 `seed/`（那是 D3 生成的产物，本步还没生成），改为自带一份**合成种子**：
用例不依赖已生成的文件、也不碰真实库（`config.DB_PATH` 被 monkeypatch 到 tmp）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src import config, db

ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    """按文件路径加载 scripts/<name>.py。

    `scripts/` 不是包（无 __init__.py），且**不宜**为测试新建包骨架 ——
    这里用 importlib 直接按路径加载，避免往 sys.path 里塞 scripts/ 造成模块名冲突。
    """
    spec = importlib.util.spec_from_file_location(f"_seed_script_{name}",
                                                  ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


load_seed = _load_script("load_seed")

# 合成种子：覆盖四张表，数值取自真实年报口径，便于人读时一眼认出对错。
BUSINESS = {
    "companies": [
        {"code": "600519", "name": "贵州茅台", "market": "sse",
         "industry": "食品饮料", "org_id": "gssh0600519"},
        {"code": "601318", "name": "中国平安", "market": "sse",
         "industry": "非银金融", "org_id": "gssh0601318"},
    ],
    "reports": [
        {"code": "600519", "year": 2024, "report_type": "年报", "title": "2024年年度报告",
         "url": "https://example.invalid/600519_2024.pdf",
         "pdf_path": "data/raw/600519/2024.pdf", "size_kb": 1234.5,
         "page_count": 88, "empty_pages": 1, "section_mode": "节号锚点",
         "parsed_at": "2025-04-01 10:00:00"},
    ],
    "financial_indicators": [
        {"code": "600519", "period": "2024-12-31", "report_type": "年报",
         "indicator": "营业总收入", "value": 1.74144e11, "unit": "元",
         "source_table": "income", "source_field": "TOTAL_OPERATE_INCOME"},
        {"code": "600519", "period": "2024-12-31", "report_type": "年报",
         "indicator": "归母净利润", "value": 8.6228e10, "unit": "元",
         "source_table": "income", "source_field": "PARENT_NETPROFIT"},
        {"code": "601318", "period": "2024-12-31", "report_type": "年报",
         "indicator": "归母净资产", "value": 9.286e11, "unit": "元",
         "source_table": "sina_balance", "source_field": "归属于母公司的股东权益合计"},
    ],
    "golden_qa": [
        {"qa_id": "ind-600519-2024-revenue",
         "question": "贵州茅台2024年的营业总收入是多少",
         "expected_answer": "1741.44 亿元", "expected_citations": "[]", "scene": "指标问答"},
    ],
}
EXPECTED = {t: len(rows) for t, rows in BUSINESS.items()}


@pytest.fixture
def seed_json(tmp_path):
    """把合成种子写到 tmp，返回路径。"""
    p = tmp_path / "business.json"
    p.write_text(json.dumps(BUSINESS, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def tmp_business_db(tmp_path, monkeypatch):
    """把业务库指向 tmp 下的临时 sqlite（空库）。"""
    path = tmp_path / "seed_business.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


def test_load_seed_counts_match_business_json(seed_json, tmp_business_db):
    """灌完后四张表行数 == business.json 里各表的行数。"""
    assert db.IS_MYSQL is False, "本用例只跑 sqlite 分支"
    counts = load_seed.load_seed(business_path=seed_json, quiet=True)
    assert counts == EXPECTED

    with db.get_conn() as conn:
        for table, n in EXPECTED.items():
            got = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert got == n, f"{table}: 库内 {got} 行 ≠ 种子 {n} 行"


def test_load_seed_is_idempotent(seed_json, tmp_business_db):
    """重复执行行数不变（upsert 命中主键 → 更新而非插入）。"""
    first = load_seed.load_seed(business_path=seed_json, quiet=True)
    second = load_seed.load_seed(business_path=seed_json, quiet=True)
    assert first == second == EXPECTED

    with db.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM financial_indicators").fetchone()["n"]
    assert n == len(BUSINESS["financial_indicators"])


def test_load_seed_keeps_per_row_provenance(seed_json, tmp_business_db):
    """口径随行落库：保险股归母净资产必须带新浪源（不是被 upsert 抹平）。"""
    load_seed.load_seed(business_path=seed_json, quiet=True)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT value, unit, source_table, source_field FROM financial_indicators "
            "WHERE code=? AND indicator=? AND period=?",
            ("601318", "归母净资产", "2024-12-31")).fetchone()
    assert row["unit"] == "元"
    assert (row["source_table"], row["source_field"]) == (
        "sina_balance", "归属于母公司的股东权益合计")
    assert abs(row["value"] - 9.286e11) < 1

    # 时间列由 now_expr() 落到 SQL 文本（不是绑定参数），故应被填成非空。
    with db.get_conn() as conn:
        ts = conn.execute(
            "SELECT fetched_at FROM financial_indicators WHERE code=? LIMIT 1",
            ("600519",)).fetchone()["fetched_at"]
    assert ts