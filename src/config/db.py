from __future__ import annotations
import json
import os
from pathlib import Path

from .core import _api_key, _db_local, _secret
from .core import ROOT_DIR, DATA_DIR, CONFIG_DIR, DB_DIR, REGULATION_DIR
# ---- 取数参数（巨潮 / 东财）----
# 反爬要点：UA 必带；新浪接口还必须带 Referer（不带直接 403）；请求间隔别太小
FETCH_UA: str = os.getenv(
    "CNINFO_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) fin-research-agent/0.1")
SINA_REFERER: str = "https://finance.sina.com.cn"
FETCH_INTERVAL: float = float(os.getenv("FETCH_INTERVAL", "1.5"))
FETCH_TIMEOUT: int = int(os.getenv("FETCH_TIMEOUT", "30"))
FETCH_RETRY: int = int(os.getenv("FETCH_RETRY", "3"))

# ---- 财务数据源注册表（Step 2，全部实测确认）----
#
# 为什么是**多源**而不是"挑一个最好的"：
# 1. **保险公司在 F10 的资产负债表/现金流量表接口下整表返回空**
#    （`success=false, message=返回数据为空`，实测 601318），只有数据中心批量报表拿得到；
#    而数据中心批量报表的字段比 F10 少（没有 TOTAL_PARENT_EQUITY）。
# 2. **东财全系都拿不到保险股的「归属于母公司股东权益」**：F10 资产负债表空、
#    数据中心资产负债简表无此列、主要指标只有 TOTAL_EQUITY_PK（含少数股东）。
#    券商 App 上能看到这一项，说明数据客观存在，只是东财不提供 → 必须引入第二源。
#    → **新浪财经**的资产负债表 JSON（`source=fzb`）有「归属于母公司的股东权益合计」，
#      实测 601318 归母 9,286.00 亿 + 少数股东 3,761.12 亿 = 13,047.12 亿，
#      与东财 TOTAL_EQUITY_PK 完全一致（恒等式零误差），且总资产/营业收入也与东财逐项对上。
#
# 所以按指标逐条声明**优先级列表**（INDICATORS[*]["sources"]），代码取第一个非空值，
# 并落**实际命中**的源（见 src/ingest/fetch_eastmoney.py 的落库约定）。
EASTMONEY_SECURITIES_API: str = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
EASTMONEY_DATACENTER_API: str = "https://datacenter-web.eastmoney.com/api/data/v1/get"
# 新浪移动端财报接口：返回「报告期列表 + 按报告期分组的项目名/值」，是结构化 JSON，
# 比解它的 HTML 财报页稳得多（HTML 表的合并单元格/表头一改版就会静默出错）。
SINA_REPORT_API: str = ("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService"
                        "/getFinanceReport2022")
SINA_REFERER: str = "https://finance.sina.com.cn"

# source_key → 用哪个 api + 哪个报表。
# `api` 取值：securities（东财 F10）/ datacenter（东财数据中心）/ sina（新浪移动端）。
# `field` 维度：东财用英文大写下划线字段名；新浪用**中文项目名**（如「归属于母公司的股东权益合计」）
# —— 两者都直接写进 INDICATORS[*]["sources"]，取数层统一按 `raw.get(field)` 取值。
DATA_SOURCES: dict[str, dict] = {
    "income":       {"api": "securities", "report_name": "RPT_F10_FINANCE_GINCOME"},
    "balance":      {"api": "securities", "report_name": "RPT_F10_FINANCE_GBALANCE"},
    "cashflow":     {"api": "securities", "report_name": "RPT_F10_FINANCE_GCASHFLOW"},
    "main":         {"api": "securities", "report_name": "RPT_F10_FINANCE_MAINFINADATA"},
    "dc_balance":   {"api": "datacenter", "report_name": "RPT_DMSK_FN_BALANCE"},
    "dc_cashflow":  {"api": "datacenter", "report_name": "RPT_DMSK_FN_CASHFLOW"},
    "dc_income":    {"api": "datacenter", "report_name": "RPT_DMSK_FN_INCOME"},
    # 新浪：`source` 是它自己的表代号。**注意是 `fzb` 不是 `zcfz`** ——
    # 实测 `source=zcfz` 返回 `{"status":{"code":0}, "data":null}`（有状态没数据，
    # 看起来像"这只股票没有资产负债表"，实际是表代号写错了）。
    "sina_balance": {"api": "sina", "source": "fzb"},   # 资产负债表
    "sina_income":  {"api": "sina", "source": "lrb"},   # 利润表
}
# 需要服务端 REPORT_TYPE 过滤的源（数据中心那批没有该字段，见上）
SOURCES_WITH_REPORT_TYPE: frozenset[str] = frozenset(
    {"income", "balance", "cashflow", "main"})
# 「补充源」：只用来**补主源缺的字段**，不参与决定"这家公司有哪几期"。
#
# 为什么必须区分：实测新浪会给到主源范围之外更早的一期（601318 主源 2020–2025 共 6 期，
# 新浪多给 2019 期）。若把并集当期间轴，就会凭空多出一个**只有新浪 3 个字段**的残缺期，
# 而其余指标在该期全部"缺失" —— 汇总时会被误报成"全源缺失（数据源未提供）"，
# 把"某期没覆盖"和"压根没这个字段"两件事混成一件，是很坏的误导。
SUPPLEMENT_APIS: frozenset[str] = frozenset({"sina"})
ANNUAL_MMDD: str = "12-31"

# REPORT_TYPE 是**中文枚举**（实测取值：一季报 / 中报 / 三季报 / 年报），可直接当过滤条件，
# 不需要靠 REPORT_DATE 的月日去猜报告期类型。
EM_REPORT_TYPE_ANNUAL: str = "年报"
# 每个公司拉取的年报期数上限（Step 2 演示用，够做同比与多年趋势即可）
EM_PERIODS: int = int(os.getenv("EM_PERIODS", "6"))

# 数据入库向导的防呆上限（设计 D6）：LLM 草稿/手填表单超出即截断，
# 防止一次误操作把抓数接口打爆或把库里灌进几千行垃圾。可按部署规模用环境变量放宽。
INGEST_MAX_COMPANIES: int = int(os.getenv("FA_INGEST_MAX_COMPANIES", "5"))
INGEST_MAX_INDICATORS: int = int(os.getenv("FA_INGEST_MAX_INDICATORS", "12"))
INGEST_MAX_PERIODS: int = int(os.getenv("FA_INGEST_MAX_PERIODS", "10"))

# 报告期类型归一表（**唯一事实来源**：取数层靠它筛源，工具层靠它认 LLM 的说法）。
# 「中报」的常见同义写法比另外三个多（半年报/半年度/H1），单独列全。
REPORT_TYPE_CANON: dict[str, str] = {
    "年报": "年报", "年度": "年报", "年度报告": "年报",
    "annual": "年报", "year": "年报", "y": "年报", "fy": "年报",
    "中报": "中报", "半年报": "中报", "半年度": "中报", "半年报(中报)": "中报",
    "semi": "中报", "h1": "中报", "semi-annual": "中报",
    "一季报": "一季报", "季报": "一季报", "q1": "一季报",
    "三季报": "三季报", "q3": "三季报",
}


def normalize_report_type(report_type: str | None) -> str:
    """报告期类型归一；认不出来退回年报（默认口径要能在返回值里体现，不能悄悄换）。

    同时被两处使用：取数层筛源（新浪按 `date_description` 文本筛）、
    工具层认 LLM 给的写法（"annual"/"年度"/"2024年报"）。放这里是为了只有一份映射。
    """
    key = (report_type or "").strip()
    if not key:
        return EM_REPORT_TYPE_ANNUAL
    return REPORT_TYPE_CANON.get(key) or REPORT_TYPE_CANON.get(key.lower()) \
        or EM_REPORT_TYPE_ANNUAL


def is_supplement_source(source_key: str) -> bool:
    """该源是否只是补充源（不定义期间轴，只在主源已确定的期上补字段）。

    补充源给到主源范围之外的期次时会**被丢掉**，而不是扩出残缺期
    —— 详见 SUPPLEMENT_APIS 上的注释。
    """
    spec = DATA_SOURCES.get(source_key) or {}
    return spec.get("api") in SUPPLEMENT_APIS


def report_type_of_description(text: str) -> str | None:
    """把新浪的期次说明（如「2024年报」「2025半年报」）映射成标准报告期类型。

    认不出返回 None（调用方应把该期丢掉，而不是猜一个类型 —— 猜错会让中报数据混进年报序列）。
    """
    t = (text or "").strip()
    if not t:
        return None
    for token in ("一季报", "半年报", "三季报", "年报"):
        if t.endswith(token):
            return REPORT_TYPE_CANON.get(token)
    return None


# ---- 数据源后端（Step 2 起启用，沿用 workflow-agent 的双后端方案）----
# 解析顺序：环境变量 FA_DB_BACKEND > data/db_keys.local.json 的 backend > sqlite
DB_BACKEND: str = (
    os.getenv("FA_DB_BACKEND", "").strip().lower()
    or _db_local("backend", "sqlite").lower()
)
CHECKPOINT_BACKEND: str = (
    os.getenv("FA_CHECKPOINT_BACKEND", "").strip().lower()
    or _db_local("checkpoint_backend", "").lower()
    or DB_BACKEND
)
DB_PATH: Path = DB_DIR / "fin_research.db"
# Checkpointer（LangGraph 挂起/恢复的落盘位置）。
#
# 为什么单独一个文件而不是复用业务库：checkpoint 是**框架的表结构**（LangGraph 自己建表、
# 自己迁移），塞进 fin_research.db 会让"业务 schema 变更"与"框架 schema 变更"互相牵连，
# 备份/清理也没法分开。文件独立后，删掉它就是"清空所有会话"，业务数据毫发无伤。
CHECKPOINT_DB_PATH: Path = DB_DIR / "checkpoints.db"
