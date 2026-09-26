"""业务库连接与 Schema —— 结构化财务数据、年报清单、审计日志、评估集。

双后端（沿用 workflow-agent 的约定，同一套工程规范）：
- `sqlite`（默认）：单机演示与**全部确定性单测**跑这套，零外部依赖；
- `mysql` ：企业部署，库外置到 MySQL 8。

切换：环境变量 `FA_DB_BACKEND=mysql`（口令见 `data/db_keys.local.json` 或 `MYSQL_PASSWORD`）。
两套 DDL **分开显式维护**：SQLite 与 MySQL 的差异（AUTOINCREMENT / TEXT 不能有 DEFAULT /
不支持 `CREATE INDEX IF NOT EXISTS` / 行内 `REFERENCES` 被 MySQL 静默忽略）无法靠字符串替换糊过去。

表设计（对应架构文档 3.2 数据层）：
- `companies`             : 标的公司清单（watchlist 落库，含巨潮 orgId）
- `reports`               : 年报文件清单（PDF 路径 + 解析状态），与 Step 1 的 data/raw、data/parsed 对应
- `financial_indicators`  : **长表**（一行一个指标一期），口径由 config.INDICATORS 收口
- `audit_logs`            : 问答与工具调用审计（Step 6 用，此处先建好）
- `golden_qa`             : 评估集（Step 6 用，此处先建好）

为什么 `financial_indicators` 用长表而不是宽表：
宽表列名要随数据源字段变而改 DDL，且「这个值取自哪张报表哪个字段」只能写死在列名里。
长表把 `source_table` / `source_field` 存成数据，**每个值都能自证口径** —— 这是本项目的立身之本。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from src import config

BACKEND = config.DB_BACKEND
IS_MYSQL = BACKEND == "mysql"

# ==================== DDL：SQLite ====================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    code        TEXT PRIMARY KEY,          -- 6 位证券代码
    name        TEXT NOT NULL,
    market      TEXT,                      -- sse / szse / bj
    industry    TEXT,
    org_id      TEXT,                      -- 巨潮 orgId（Step 1 取数用）
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS reports (
    code        TEXT NOT NULL,
    year        INTEGER NOT NULL,
    report_type TEXT NOT NULL DEFAULT 'annual',
    title       TEXT,
    url         TEXT,
    pdf_path    TEXT,
    size_kb     REAL,
    page_count  INTEGER,
    empty_pages INTEGER,
    section_mode TEXT,                     -- 章节识别走的哪条路（节号锚点 / 页首标题）
    parsed_at   TEXT,
    PRIMARY KEY (code, year, report_type)
);

-- 长表：一行 = 一个指标的一期值。source_table/source_field 随行存储，让每个值自带口径。
CREATE TABLE IF NOT EXISTS financial_indicators (
    code         TEXT NOT NULL,
    period       TEXT NOT NULL,            -- 报告期 ISO 日期，如 2024-12-31
    report_type  TEXT NOT NULL,            -- 年报 / 中报 / 一季报 / 三季报
    indicator    TEXT NOT NULL,            -- 标准指标名（config.INDICATORS 的键）
    value        REAL,
    unit         TEXT,
    source_table TEXT,                     -- income / balance / cashflow / main
    source_field TEXT,                     -- 东财原始字段名，可回溯核对
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (code, period, report_type, indicator)
);

CREATE INDEX IF NOT EXISTS idx_fi_code_indicator ON financial_indicators(code, indicator);
CREATE INDEX IF NOT EXISTS idx_fi_period ON financial_indicators(period);
CREATE INDEX IF NOT EXISTS idx_reports_code ON reports(code);

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    actor       TEXT,                      -- 用户/会话标识
    action      TEXT NOT NULL,             -- ask / tool_call / retrieve ...
    target      TEXT,                      -- 问题或工具名
    detail_json TEXT,                      -- 入参出参快照（脱敏后）
    latency_ms  INTEGER
);

CREATE TABLE IF NOT EXISTS golden_qa (
    qa_id               TEXT PRIMARY KEY,
    question            TEXT NOT NULL,
    expected_answer     TEXT,
    expected_citations  TEXT,              -- JSON 数组：[{code,year,page_no,section}]
    scene               TEXT,              -- 指标问答 / 原文检索 / 合规核查
    created_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 用户表（本轮新增）：登录与 JWT 的身份来源。
-- 口令**不存明文、也不存可逆加密**：存 pbkdf2 派生值 + 每用户独立盐（见 src/auth.py）。
-- `disabled` 用 0/1 而不是布尔字面量：SQLite 无 BOOLEAN，MySQL 对应 TINYINT，两端写法统一。
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,           -- pbkdf2_hmac('sha256') 派生值的 hex
    salt          TEXT NOT NULL,           -- 每用户独立盐的 hex（不用全局盐）
    role          TEXT NOT NULL DEFAULT 'analyst',
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# ==================== DDL：MySQL 8 ====================
# 与 SQLite 版的差异都写在注释里，便于对照：
# - TEXT/BLOB 列不允许有 DEFAULT → 时间列改 DATETIME DEFAULT CURRENT_TIMESTAMP
# - 行内 REFERENCES 被静默忽略 → 需要外键时用表级 FOREIGN KEY（本库表间无强外键，靠 code 关联）
# - 不支持 CREATE INDEX IF NOT EXISTS → 由 _ensure_mysql_indexes() 查 information_schema 补建
# - DOUBLE 对应 SQLite 的 REAL

SCHEMA_MYSQL_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS companies (
        code       VARCHAR(16) PRIMARY KEY,
        name       VARCHAR(128) NOT NULL,
        market     VARCHAR(16) NULL,
        industry   VARCHAR(64) NULL,
        org_id     VARCHAR(64) NULL,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS reports (
        code         VARCHAR(16) NOT NULL,
        year         INT NOT NULL,
        report_type  VARCHAR(16) NOT NULL DEFAULT 'annual',
        title        VARCHAR(512) NULL,
        url          VARCHAR(512) NULL,
        pdf_path     VARCHAR(512) NULL,
        size_kb      DOUBLE NULL,
        page_count   INT NULL,
        empty_pages  INT NULL,
        section_mode VARCHAR(32) NULL,
        parsed_at    VARCHAR(32) NULL,
        PRIMARY KEY (code, year, report_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_indicators (
        code         VARCHAR(16) NOT NULL,
        period       VARCHAR(20) NOT NULL,
        report_type  VARCHAR(16) NOT NULL,
        indicator    VARCHAR(64) NOT NULL,
        value        DOUBLE NULL,
        unit         VARCHAR(16) NULL,
        source_table VARCHAR(16) NULL,
        source_field VARCHAR(64) NULL,
        fetched_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (code, period, report_type, indicator)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        log_id      INT AUTO_INCREMENT PRIMARY KEY,
        created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actor       VARCHAR(64) NULL,
        action      VARCHAR(32) NOT NULL,
        target      VARCHAR(512) NULL,
        detail_json MEDIUMTEXT NULL,
        latency_ms  INT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS golden_qa (
        qa_id              VARCHAR(64) PRIMARY KEY,
        question           VARCHAR(1024) NOT NULL,
        expected_answer    MEDIUMTEXT NULL,
        expected_citations TEXT NULL,
        scene              VARCHAR(32) NULL,
        created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # 与 SQLite 版的差异：INTEGER→TINYINT（布尔）、TEXT 主键要指定长度才能建索引、
    # 时间列 DATETIME DEFAULT CURRENT_TIMESTAMP。username 的唯一性用**具名**表级
    # UNIQUE KEY 声明（具名是为了冲突报错里能看出是哪条约束，也便于日后 DROP INDEX）。
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id       VARCHAR(64) PRIMARY KEY,
        username      VARCHAR(128) NOT NULL,
        password_hash VARCHAR(256) NOT NULL,
        salt          VARCHAR(64) NOT NULL,
        role          VARCHAR(32) NOT NULL DEFAULT 'analyst',
        disabled      TINYINT NOT NULL DEFAULT 0,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_users_username (username)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

SCHEMA_MYSQL_INDEXES = [
    ("financial_indicators", "idx_fi_code_indicator", "(`code`, `indicator`)"),
    ("financial_indicators", "idx_fi_period", "(`period`)"),
    ("reports", "idx_reports_code", "(`code`)"),
]

# 清库顺序无关紧要（无强外键），但要覆盖全部业务表
ALL_TABLES = ("financial_indicators", "reports", "companies", "audit_logs", "golden_qa",
              "users")


# ==================== 连接层 ====================

class _MySQLConn:
    """把 pymysql 连接包装成与 sqlite3.Connection 同形的接口。

    只做两件事：占位符 `?` → `%s`（让全项目 SQL 只写一份），行转 dict。
    cursor 本身已支持 fetchone() / 迭代 / lastrowid，与 sqlite3.Cursor 一致。
    """

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        cur = self._raw.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executemany(self, sql: str, seq):
        cur = self._raw.cursor()
        cur.executemany(sql.replace("?", "%s"), seq)
        return cur

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


# 哨兵：区分「调用方没传 database」与「显式传 None（= 不要库）」，见 _connect_mysql
_USE_CONFIG_DB = object()


def _connect_mysql(database: str | None = _USE_CONFIG_DB):
    """连 MySQL。`database` 不传 → 用 config 里配的库；显式传 None → **不指定库**。

    ⚠️ 必须区分「不传」与「传 None」：`ensure_mysql_database()` 要在库**还不存在**时
    连服务器执行 `CREATE DATABASE`，此时**不能**在连接参数里带 `database`，
    否则 pymysql 会在握手阶段就报 `1049 Unknown database`（本步实测踩到）。
    早先的实现把 None 当成"用默认库"，导致建库前永远连不上 —— 首次部署必挂。
    """
    import pymysql
    from pymysql.cursors import DictCursor

    cfg = dict(config.MYSQL)
    if database is not _USE_CONFIG_DB:
        cfg["database"] = database
    return pymysql.connect(charset="utf8mb4", cursorclass=DictCursor, **cfg)


def ensure_mysql_database() -> None:
    """库不存在则创建（连服务器时不指定 database）。"""
    db_name = config.MYSQL["database"]
    raw = _connect_mysql(database=None)
    try:
        with raw.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        raw.commit()
    finally:
        raw.close()


def _ensure_mysql_indexes(conn) -> None:
    """MySQL 不支持 CREATE INDEX IF NOT EXISTS，查 information_schema 补建。"""
    with conn.cursor() as cur:
        for table, index, cols in SCHEMA_MYSQL_INDEXES:
            cur.execute(
                """SELECT COUNT(*) AS n FROM information_schema.statistics
                   WHERE table_schema=%s AND table_name=%s AND index_name=%s""",
                (config.MYSQL["database"], table, index),
            )
            if cur.fetchone()["n"] == 0:
                cur.execute(f"CREATE INDEX {index} ON {table} {cols}")
    conn.commit()


# ==================== 对外接口（两后端同形） ====================

def init_schema(db_path: Path | None = None) -> None:
    """建表（幂等）。sqlite 用 db_path；mysql 忽略该参数，库与表按 config 建。"""
    if IS_MYSQL:
        ensure_mysql_database()
        raw = _connect_mysql()
        try:
            with raw.cursor() as cur:
                for ddl in SCHEMA_MYSQL_TABLES:
                    cur.execute(ddl)
            raw.commit()
            _ensure_mysql_indexes(raw)
        finally:
            raw.close()
        return

    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA_SQL)


@contextmanager
def get_conn(db_path: Path | None = None):
    """拿连接（行转 dict），退出时提交。"""
    if IS_MYSQL:
        conn = _MySQLConn(_connect_mysql())
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
        return

    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_expr() -> str:
    """SQL 里的「当前时间」表达式（两后端不同）。

    注意：**必须拼进 SQL 文本，不能当绑定参数传** —— pymysql 会把 'NOW()'
    转义成字符串字面量，MySQL 直接报
    `1292 Incorrect datetime value: 'NOW()'`（继承自 workflow-agent 的坑）。
    """
    return "NOW()" if IS_MYSQL else "datetime('now','localtime')"


def upsert_companies_sql() -> str:
    """公司清单 upsert（按 code 幂等）。"""
    cols = "code, name, market, industry, org_id, updated_at"
    marks = f"?, ?, ?, ?, ?, {now_expr()}"
    if IS_MYSQL:
        return f"""INSERT INTO companies ({cols}) VALUES ({marks}) AS new
                   ON DUPLICATE KEY UPDATE
                     name=new.name, market=new.market, industry=new.industry,
                     org_id=new.org_id, updated_at=new.updated_at"""
    sets = ", ".join(f"{c}=excluded.{c}" for c in
                     ("name", "market", "industry", "org_id", "updated_at"))
    return f"""INSERT INTO companies ({cols}) VALUES ({marks})
               ON CONFLICT(code) DO UPDATE SET {sets}"""


def upsert_indicators_sql() -> str:
    """财务指标 upsert（按 code+period+report_type+indicator 幂等）。

    用 executemany 批量灌，所以这里返回的是单行占位符形式。
    """
    cols = ("code, period, report_type, indicator, value, unit, "
            "source_table, source_field, fetched_at")
    marks = f"?, ?, ?, ?, ?, ?, ?, ?, {now_expr()}"
    if IS_MYSQL:
        return f"""INSERT INTO financial_indicators ({cols}) VALUES ({marks}) AS new
                   ON DUPLICATE KEY UPDATE
                     value=new.value, unit=new.unit, source_table=new.source_table,
                     source_field=new.source_field, fetched_at=new.fetched_at"""
    sets = ", ".join(f"{c}=excluded.{c}" for c in
                     ("value", "unit", "source_table", "source_field", "fetched_at"))
    return f"""INSERT INTO financial_indicators ({cols}) VALUES ({marks})
               ON CONFLICT(code, period, report_type, indicator) DO UPDATE SET {sets}"""


def upsert_report_sql() -> str:
    """年报清单 upsert（按 code+year+report_type 幂等）。"""
    # 列名先写成列表再 join：`len("a, b, c")` 是**字符串长度**而不是列数，
    # 直接对字符串取 len 会生成几百个占位符，报「104 values for 11 columns」这种怪错。
    cols_list = ["code", "year", "report_type", "title", "url", "pdf_path", "size_kb",
                 "page_count", "empty_pages", "section_mode", "parsed_at"]
    cols = ", ".join(cols_list)
    marks = ", ".join(["?"] * len(cols_list))
    if IS_MYSQL:
        return f"""INSERT INTO reports ({cols}) VALUES ({marks}) AS new
                   ON DUPLICATE KEY UPDATE
                     title=new.title, url=new.url, pdf_path=new.pdf_path,
                     size_kb=new.size_kb, page_count=new.page_count,
                     empty_pages=new.empty_pages, section_mode=new.section_mode,
                     parsed_at=new.parsed_at"""
    sets = ", ".join(f"{c}=excluded.{c}" for c in
                     ("title", "url", "pdf_path", "size_kb", "page_count",
                      "empty_pages", "section_mode", "parsed_at"))
    return f"""INSERT INTO reports ({cols}) VALUES ({marks})
               ON CONFLICT(code, year, report_type) DO UPDATE SET {sets}"""


def upsert_user_sql() -> str:
    """用户 upsert（按 user_id 幂等），**口令列刻意不在更新集里**。

    为什么更新集里没有 `password_hash` / `salt`：这个方法只用于「种子账号幂等刷存在」，
    若把口令也算进更新集，每次重启都会用配置里的口令覆盖掉已被改过的口令 ——
    表现形式是「昨天改的密码，今天重启服务后又变回原样」，极难归因。
    真要有"重置口令"的需求，应该走单独的显式流程，而不是顺带在启动时发生。

    `role` / `updated_at` 在冲突时会被刷新，这样"把 admin 降级成 analyst"改配置后重启即生效。
    """
    cols = "user_id, username, password_hash, salt, role, disabled, updated_at"
    marks = f"?, ?, ?, ?, ?, ?, {now_expr()}"
    if IS_MYSQL:
        return f"""INSERT INTO users ({cols}) VALUES ({marks}) AS new
                   ON DUPLICATE KEY UPDATE
                     role=new.role, disabled=new.disabled, updated_at=new.updated_at"""
    return f"""INSERT INTO users ({cols}) VALUES ({marks})
               ON CONFLICT(user_id) DO UPDATE SET
                 role=excluded.role, disabled=excluded.disabled,
                 updated_at=excluded.updated_at"""


def get_user(username: str, db_path: Path | None = None) -> dict | None:
    """按用户名查用户（返回 dict；不存在或已禁用由调用方决定如何处置）。

    刻意**不在这里过滤 `disabled`**：登录失败原因的区分放在 auth 层做统一处理
    （见 auth.authenticate —— 三种失败一律回 None，避免用户名枚举），
    数据层只管如实取数。
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, username, password_hash, salt, role, disabled, created_at "
            "FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        return None
    return dict(row)


def insert_user(user_id: str, username: str, password_hash: str, salt: str,
                *, role: str = "analyst", disabled: int = 0,
                db_path: Path | None = None) -> None:
    """插入用户。用户名已被占用时由数据库抛唯一约束错（调用方转成 409）。

    `user_id` 由调用方生成而不是这里生成：本项目其余 ID（如 golden_qa.qa_id）也是
    由业务层决定形态，数据层只负责落库，这样 ID 规则改动不需要动 DDL 访问函数。
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, salt, role, disabled) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, password_hash, salt, role, disabled))


def reset_business_tables() -> None:
    """清空业务表（仅开发期 --reset 用）。mysql 走 TRUNCATE；sqlite 由调用方删库文件。"""
    if not IS_MYSQL:
        return
    raw = _connect_mysql()
    try:
        with raw.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in ALL_TABLES:
                cur.execute(f"TRUNCATE TABLE {table}")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        raw.commit()
    finally:
        raw.close()


if __name__ == "__main__":
    # 自检：python -m src.db（建表 + 报告表结构）
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    init_schema()
    print(f"后端 {BACKEND}；库路径 {config.DB_PATH if not IS_MYSQL else config.MYSQL}")
    with get_conn() as conn:
        for t in ALL_TABLES:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
            n = row["n"] if isinstance(row, dict) else row[0]
            print(f"  {t:<24} {n} 行")
    print("建表自检通过")
