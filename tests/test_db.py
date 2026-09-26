"""结构化业务库（src/db.py）测试。

覆盖点刻意选在「换后端会踩到」和「幂等性」上，而不是去测 SQL 语法本身：
- upsert 必须幂等（重复灌同一批数据不能让行数翻倍、也不能报错）——
  取数是可重复执行的动作，幂等破了就会出现"跑两次数据翻倍"的脏库。
- `now_expr()` 必须进 SQL 文本而不是当绑定参数（这是继承自 workflow-agent 的坑，
  MySQL 下把 'NOW()' 当参数传会直接报 1292）。
- 占位符统一写 `?`（由 _MySQLConn 转 `%s`），所以任何一句 SQL 里出现 `%s`
  都说明有人绕过封装写了后端相关的代码。
"""
from __future__ import annotations

from src import config, db
from tests.conftest import SYNTH_COMPANIES, SYNTH_INDICATORS, SYNTH_REPORT_TYPE


def test_init_schema_is_idempotent(synth_db):
    """重复建表不应报错，也不应清掉已有数据。"""
    db.init_schema(db_path=synth_db)
    with db.get_conn(db_path=synth_db) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
    assert n == len(SYNTH_COMPANIES)


def test_upsert_indicators_is_idempotent(synth_db):
    """同一批指标灌两次，行数不变（primary key 命中即更新）。"""
    with db.get_conn(db_path=synth_db) as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM financial_indicators").fetchone()["n"]
        conn.executemany(
            db.upsert_indicators_sql(),
            [(c, p, SYNTH_REPORT_TYPE, i, v, u, st, sf)
             for c, p, i, v, u, st, sf in SYNTH_INDICATORS])
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM financial_indicators").fetchone()["n"]
    assert before == after == len(SYNTH_INDICATORS)


def test_upsert_indicators_updates_value_in_place(synth_db):
    """重复灌时值应被更新（而不是插入重复行）。"""
    with db.get_conn(db_path=synth_db) as conn:
        conn.execute(db.upsert_indicators_sql(),
                     ("600519", "2024-12-31", SYNTH_REPORT_TYPE, "营业收入",
                      9.99e10, "元", "income", "OPERATE_INCOME"))
        rows = conn.execute(
            "SELECT value FROM financial_indicators WHERE code=? AND period=? "
            "AND report_type=? AND indicator=?",
            ("600519", "2024-12-31", SYNTH_REPORT_TYPE, "营业收入")).fetchall()
    assert len(rows) == 1
    assert abs(rows[0]["value"] - 9.99e10) < 1


def test_source_columns_are_stored_per_row(synth_db):
    """每个值都要能自证口径：source_table / source_field 必须落库、可回查。"""
    with db.get_conn(db_path=synth_db) as conn:
        row = conn.execute(
            "SELECT value, unit, source_table, source_field FROM financial_indicators "
            "WHERE code=? AND indicator=? AND period=?",
            ("601318", "总资产", "2024-12-31")).fetchone()
    assert row["unit"] == "元"
    # 保险股总资产走的是 dc_balance（F10 为空），这条断言就是在盯住"实际命中的源"
    assert (row["source_table"], row["source_field"]) == ("dc_balance", "TOTAL_ASSETS")


def test_now_expr_is_sql_text_not_parameter():
    """`NOW()` / `datetime(...)` 必须作为 SQL 文本拼入，不能是 `?` 参数。

    只有带 `fetched_at` / `updated_at` 时间列的表会用到它 —— `reports` 表没有时间列
    （`parsed_at` 由调用方显式传值），所以不在断言范围内。
    """
    expr = db.now_expr()
    assert "?" not in expr
    assert "NOW()" in expr or "datetime" in expr
    for sql in (db.upsert_companies_sql(), db.upsert_indicators_sql()):
        assert expr in sql, f"时间表达式未作为 SQL 文本出现：{sql[:80]}"
    assert expr not in db.upsert_report_sql()


def test_sql_uses_question_mark_placeholders_only():
    """全项目 SQL 只写 `?`；出现 `%s` 说明有人绕过 _MySQLConn 写了后端相关代码。"""
    for sql in (db.upsert_companies_sql(), db.upsert_indicators_sql(),
                db.upsert_report_sql()):
        assert "%s" not in sql


def test_report_upsert_marks_match_columns():
    """回归：`len("a, b, c")` 是字符串长度（4），不是列数（3）。

    这个 bug 曾生成上百个占位符，报 `104 values for 11 columns`。
    这里直接核对「占位符个数 == 列数」，把这类错误挡在单测里。
    """
    sql = db.upsert_report_sql()
    head = sql.split("VALUES", 1)[0]
    cols = head[head.index("(") + 1:head.index(")")].split(",")
    n_marks = sql.split("VALUES", 1)[1].count("?")
    assert len(cols) == 11, f"reports 应为 11 列，实为 {len(cols)}"
    assert n_marks == len(cols), f"占位符 {n_marks} 个 ≠ 列数 {len(cols)}"


def test_backend_defaults_to_sqlite_in_tests():
    """单测必须跑 sqlite：conftest 已在导入前设好环境变量。"""
    assert config.DB_BACKEND == "sqlite"
    assert db.IS_MYSQL is False
