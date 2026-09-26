"""pytest 全局配置。

沿用 workflow-agent 的约定：**单测一律跑 SQLite**，不许意外打到真实 MySQL，
也必须在任何 `import src.*` 之前设好（配置在导入时即解析）。

Step 1 的用例全部是纯函数（不联网、不落盘），所以只需要保证：
- 项目根在 sys.path 上；
- 取数节流为 0（万一有用例碰到网络也不至于被 1.5s 间隔拖慢）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("FA_DB_BACKEND", "sqlite")
os.environ.setdefault("FA_CHECKPOINT_BACKEND", "sqlite")
os.environ.setdefault("FETCH_INTERVAL", "0")
# 鉴权与联网搜索默认**关**，让既有确定性用例继续跑"免鉴权 + 不联网"形态，
# 一条都不用改语义。这两条通道的契约由专门的用例覆盖：
#   - 鉴权：tests/test_auth.py（模块契约）+ tests/test_server_auth.py（HTTP 面）
#   - 联网搜索：tests/test_web_search.py（纯函数与假 provider，绝不联网）
# 生产默认值是相反的（AUTH_ENABLED=1 是安全默认，见 config.py）——
# 单测要的是确定性与零外部依赖，不是"和生产同构"。
os.environ.setdefault("FA_AUTH_ENABLED", "0")
os.environ.setdefault("FA_WEB_SEARCH_ENABLED", "0")

import pytest  # noqa: E402


@pytest.fixture
def synth_pdf(tmp_path):
    """造一份可控的小 PDF，用于验证「页码 / 章节 / 页眉页脚」三件事。

    刻意做成 6 页并带目录页，因为这正是真实年报里最容易出错的形态：
    - 第 1 页是目录（一次列全 10 个章节）→ 必须被判为目录页、不能生效
    - 每页都有页脚「测试公司2024年年度报告 第N页」→ 必须被清理掉
    """
    import pymupdf

    sections = [
        "重要提示、目录和释义", "公司简介和主要财务指标", "管理层讨论与分析",
        "公司治理", "环境和社会责任", "重要事项", "股份变动及股东情况",
        "优先股相关情况", "债券相关情况", "财务报告",
    ]
    body = "本报告期内公司实现营业总收入与上年同期相比保持稳定增长态势。" * 3
    plan = [
        None,                                  # p1 目录
        ("第一节 重要提示、目录和释义", body),
        (None, body),                          # p3 续 p2 的章节
        ("第三节 管理层讨论与分析", body),
        (None, body),
        (None, body),
    ]

    doc = pymupdf.open()
    for idx, item in enumerate(plan, start=1):
        page = doc.new_page()
        y = 72
        if idx == 1:
            for i, s in enumerate(sections, start=1):
                page.insert_text((72, y), f"第{i}节 {s}", fontname="china-s", fontsize=11)
                y += 16
        else:
            heading, text = item
            if heading:
                page.insert_text((72, y), heading, fontname="china-s", fontsize=14)
                y += 24
            page.insert_text((72, y), text, fontname="china-s", fontsize=11)
        # 页脚（跨页重复，应被清理）
        page.insert_text((72, 780), f"测试公司2024年年度报告 第{idx}页",
                         fontname="china-s", fontsize=9)
    path = tmp_path / "600000_2024_annual.pdf"
    doc.save(path)
    doc.close()
    return path


# ==================== Step 2：结构化库 / 工具层 的测试夹具 ====================

# 为什么用**合成数**而不是真实取数来测：
# 这批用例要断言的是「计算规则与选期规则」，不是「东财给的数据对不对」。
# 合成数让每个期望值都能手算复现，而且能构造真实数据里难得一见的边界 ——
# 缺分项（新出年报只补了营业收入、还没补营业成本）、各公司最新期不一致、
# 公司简称互相包含（"平安"既是平安银行也是中国平安）。
# 真实数据留给 scripts/verify_step2_db.py 做端到端数值核对。
SYNTH_COMPANIES = [
    ("600519", "贵州茅台", "sse", "食品饮料"),
    ("000858", "五粮液", "szse", "食品饮料"),
    ("601318", "中国平安", "sse", "非银金融"),
    ("000001", "平安银行", "szse", "银行"),
]

SYNTH_REPORT_TYPE = "年报"

# (code, period, indicator, value, unit, source_table, source_field)
SYNTH_INDICATORS = [
    # ---- 600519 贵州茅台：2024 期分项齐全（可手算校验各比率口径）----
    # 毛利率 = (1.0e11 − 4.0e10)/1.0e11 = 60.00%     官方 60.00 → 一致
    # 净利率 = 2.1e10/1.02e11 = 20.588%              官方 20.59 → 一致
    # 资产负债率 = 1.2e11/2.0e11 = 60.00%            官方 60.00 → 一致
    # ROE(期末) = 2.0e10/1.0e11 = 20.00%             官方 17.50 → 差 2.50pp → 差异较大
    # 经营现金流净利润比 = 3.0e10/2.0e10 = 1.50 倍   无官方口径
    ("600519", "2024-12-31", "营业收入", 1.00e11, "元", "income", "OPERATE_INCOME"),
    ("600519", "2024-12-31", "营业总收入", 1.02e11, "元", "income", "TOTAL_OPERATE_INCOME"),
    ("600519", "2024-12-31", "营业成本", 4.00e10, "元", "income", "OPERATE_COST"),
    ("600519", "2024-12-31", "归母净利润", 2.00e10, "元", "income", "PARENT_NETPROFIT"),
    ("600519", "2024-12-31", "净利润", 2.10e10, "元", "income", "NETPROFIT"),
    ("600519", "2024-12-31", "总资产", 2.00e11, "元", "balance", "TOTAL_ASSETS"),
    ("600519", "2024-12-31", "总负债", 1.20e11, "元", "balance", "TOTAL_LIABILITIES"),
    ("600519", "2024-12-31", "归母净资产", 1.00e11, "元", "balance", "TOTAL_PARENT_EQUITY"),
    ("600519", "2024-12-31", "所有者权益合计", 8.00e10, "元", "balance", "TOTAL_EQUITY"),
    ("600519", "2024-12-31", "经营活动现金流净额", 3.00e10, "元", "cashflow", "NETCASH_OPERATE"),
    ("600519", "2024-12-31", "毛利率", 60.00, "%", "main", "XSMLL"),
    ("600519", "2024-12-31", "净利率", 20.59, "%", "main", "XSJLL"),
    ("600519", "2024-12-31", "资产负债率", 60.00, "%", "main", "ZCFZL"),
    ("600519", "2024-12-31", "ROE", 17.50, "%", "main", "ROEJQ"),
    # 2023 期：只放比率所需的最少分项，用来验证"序列按期倒序"
    ("600519", "2023-12-31", "营业收入", 9.00e10, "元", "income", "OPERATE_INCOME"),
    ("600519", "2023-12-31", "营业成本", 3.60e10, "元", "income", "OPERATE_COST"),
    ("600519", "2023-12-31", "归母净利润", 1.80e10, "元", "income", "PARENT_NETPROFIT"),
    ("600519", "2023-12-31", "归母净资产", 9.00e10, "元", "balance", "TOTAL_PARENT_EQUITY"),
    # 2025 期：**只有营业收入**（模拟"最新年报数据只补了一半"）
    # → 算毛利率时必须跳过它、回退到分项最齐的 2024 期，并在 period_note 里说明
    ("600519", "2025-12-31", "营业收入", 1.10e11, "元", "income", "OPERATE_INCOME"),

    # ---- 000858 五粮液：只有 2024 期，用来验证"各公司期次不一致"的对比告警 ----
    ("000858", "2024-12-31", "营业收入", 8.00e10, "元", "income", "OPERATE_INCOME"),
    ("000858", "2024-12-31", "归母净利润", 2.80e10, "元", "income", "PARENT_NETPROFIT"),
    ("000858", "2024-12-31", "归母净资产", 1.40e11, "元", "balance", "TOTAL_PARENT_EQUITY"),
    ("000858", "2024-12-31", "ROE", 20.00, "%", "main", "ROEJQ"),

    # ---- 601318 中国平安：**保险口径**，按真实数据的形态构造 ----
    # 关键：归母净资产**必须来自新浪**（`sina_balance`）—— 这是本夹具最想固定下来的事实：
    # 东财全系都不给保险股的「归属于母公司股东权益」，只有第二源有。
    # 若哪天有人把「归母净资产」的 sources 改回只用东财，这个用例会红。
    #
    # 刻意**没有**的项：营业成本（保险业无此科目，成本行是「营业支出」）、净利润、毛利率。
    # 资产负债率 = 1.10e13/1.20e13 = 91.6667%  官方 91.67 → 一致
    # 经营现金流净利润比 = 3.80e11/1.20e11 = 3.1667 倍
    # 保险毛利率 = (1.00e12 − 8.20e11)/1.00e12 = 18.00%
    # ROE(期末) = 1.20e11/9.00e11 = 13.3333%   官方 13.80 → 差 0.4667pp → 口径差异
    ("601318", "2024-12-31", "营业总收入", 1.00e12, "元", "income", "TOTAL_OPERATE_INCOME"),
    ("601318", "2024-12-31", "营业收入", 1.00e12, "元", "income", "OPERATE_INCOME"),
    ("601318", "2024-12-31", "营业支出", 8.20e11, "元", "income", "TOTAL_OPERATE_COST"),
    ("601318", "2024-12-31", "归母净利润", 1.20e11, "元", "income", "PARENT_NETPROFIT"),
    ("601318", "2024-12-31", "总资产", 1.20e13, "元", "dc_balance", "TOTAL_ASSETS"),
    ("601318", "2024-12-31", "总负债", 1.10e13, "元", "dc_balance", "TOTAL_LIABILITIES"),
    ("601318", "2024-12-31", "所有者权益合计", 1.00e12, "元", "dc_balance", "TOTAL_EQUITY"),
    ("601318", "2024-12-31", "归母净资产", 9.00e11, "元",
     "sina_balance", "归属于母公司的股东权益合计"),
    ("601318", "2024-12-31", "经营活动现金流净额", 3.80e11, "元", "dc_cashflow", "NETCASH_OPERATE"),
    ("601318", "2024-12-31", "资产负债率", 91.67, "%", "main", "ZCFZL"),
    ("601318", "2024-12-31", "ROE", 13.80, "%", "main", "ROEJQ"),

    # ---- 000001 平安银行：只有归母净利润，用来覆盖「ROE 缺分母(归母净资产)」的分支 ----
    ("000001", "2024-12-31", "归母净利润", 5.00e10, "元", "income", "PARENT_NETPROFIT"),
]


@pytest.fixture
def synth_db(tmp_path, monkeypatch):
    """建一个临时 sqlite 库并灌入合成数据，返回库路径。

    `monkeypatch.setattr(config, "DB_PATH", ...)` 能生效，是因为 `db.get_conn()`
    是在**调用时**读 `config.DB_PATH`（而不是导入时快照），所以运行期改得动。
    """
    from src import config, db

    path = tmp_path / "test_fin.db"
    monkeypatch.setattr(config, "DB_PATH", path)

    db.init_schema(db_path=path)
    with db.get_conn(db_path=path) as conn:
        conn.executemany(
            db.upsert_companies_sql(),
            [(c, n, m, ind, f"org{c}") for c, n, m, ind in SYNTH_COMPANIES])
        conn.executemany(
            db.upsert_indicators_sql(),
            [(code, period, SYNTH_REPORT_TYPE, indicator, value, unit, st, sf)
             for code, period, indicator, value, unit, st, sf in SYNTH_INDICATORS])
    return path
