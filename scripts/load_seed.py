"""载入种子 —— 把 `seed/business.json` 灌进**当前后端**（SQLite 或 MySQL 走同一份代码）。

为什么不做 mysqldump / sqlite `.dump`：
导出文件会绑死一种后端，双容器（app + mysql）与本地 sqlite 演示就没法共用一份种子。
这里读的是 `export_seed.py` 写的**后端中立 JSON**，经 `src/db.py` 的 upsert 语句落库 ——
**换后端只改一个环境变量** `FA_DB_BACKEND`，脚本与种子文件都不用动。

幂等性：全部按主键 upsert（`ON CONFLICT … DO UPDATE` / `ON DUPLICATE KEY UPDATE`），
所以容器每次重启都跑一遍也安全 —— 行数不会翻倍，值会被刷新成种子里的最新值。

用法：
    python scripts/load_seed.py                    # 灌 seed/business.json 到当前后端
    python scripts/load_seed.py --dry-run          # 只读种子并打印将写入的行数
    FA_DB_BACKEND=mysql python scripts/load_seed.py

⚠️ 语料镜像（seed/data/…）不在这里灌 —— 那是**文件**，由 entrypoint.sh 用 `cp -rn` 处理
（`-n` 不覆盖：用户挂卷放了真语料时以卷为准）。本脚本只管**数据库表**。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import db  # noqa: E402

# 表 → upsert 的**值列**（不含时间列：时间列由 db.now_expr() 拼进 SQL，不走绑定参数，
# 否则 MySQL 会把 'NOW()' 当字符串字面量，报 1292）。顺序须与各 upsert 语句的占位符一致。
INSERT_COLS: dict[str, tuple[str, ...]] = {
    "companies": ("code", "name", "market", "industry", "org_id"),
    "reports": ("code", "year", "report_type", "title", "url", "pdf_path", "size_kb",
                "page_count", "empty_pages", "section_mode", "parsed_at"),
    "financial_indicators": ("code", "period", "report_type", "indicator", "value",
                             "unit", "source_table", "source_field"),
    "golden_qa": ("qa_id", "question", "expected_answer", "expected_citations", "scene"),
}

# 依依赖方向载入（公司 → 年报清单 → 指标 → 评估集）：本库无强外键，顺序只为日志易读。
LOAD_ORDER = ("companies", "reports", "financial_indicators", "golden_qa")


def _golden_qa_upsert_sql() -> str:
    """golden_qa 的 upsert 语句（按 qa_id 幂等）。

    `src/db.py` 只为 companies/reports/financial_indicators 备了 upsert 语句，
    golden_qa 是本步新增的种子表；把它的方言判断放这里，**避免为一步扩大 db.py 的改动面**。
    仍复用 `db.now_expr()` 与 `db.IS_MYSQL`，SQL 占位符一律写 `?`（由 _MySQLConn 转 `%s`）。
    """
    cols = "qa_id, question, expected_answer, expected_citations, scene, created_at"
    marks = f"?, ?, ?, ?, ?, {db.now_expr()}"
    if db.IS_MYSQL:
        return f"""INSERT INTO golden_qa ({cols}) VALUES ({marks}) AS new
                   ON DUPLICATE KEY UPDATE
                     question=new.question, expected_answer=new.expected_answer,
                     expected_citations=new.expected_citations, scene=new.scene,
                     created_at=new.created_at"""
    sets = ", ".join(f"{c}=excluded.{c}" for c in
                     ("question", "expected_answer", "expected_citations", "scene",
                      "created_at"))
    return f"""INSERT INTO golden_qa ({cols}) VALUES ({marks})
               ON CONFLICT(qa_id) DO UPDATE SET {sets}"""


def _upsert_sql(table: str) -> str:
    if table == "companies":
        return db.upsert_companies_sql()
    if table == "reports":
        return db.upsert_report_sql()
    if table == "financial_indicators":
        return db.upsert_indicators_sql()
    return _golden_qa_upsert_sql()


def load_seed(business_path: Path | None = None, *, quiet: bool = False) -> dict[str, int]:
    """把种子灌进当前后端，返回**载入后**各表行数。先建表（幂等）再按表 upsert。"""
    path = Path(business_path or ROOT / "seed" / "business.json")
    business = json.loads(path.read_text(encoding="utf-8"))

    db.init_schema()   # 建表（幂等）：种子只负责数据，不负责 schema

    with db.get_conn() as conn:
        for table in LOAD_ORDER:
            rows = business.get(table) or []
            if rows:
                cols = INSERT_COLS[table]
                conn.executemany(
                    _upsert_sql(table),
                    [tuple(row.get(c) for c in cols) for row in rows])
        # 行数在**载入后**从库里数（而不是数种子里有多少行）——
        # 这样才能证明"重复执行行数不变"（种子行数 + 已存在行的并集 = 库内实际行数）。
        counts = {}
        for table in LOAD_ORDER:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"] if isinstance(row, dict) else row[0])

    if not quiet:
        print(f"已从 {path} 灌入后端 {db.BACKEND}")
        print("=== 库内各表行数（载入后）===")
        for table in LOAD_ORDER:
            print(f"  {table:<22} {counts[table]:>6} 行")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="把 seed/business.json 灌进当前后端")
    ap.add_argument("--seed", default=None, help="business.json 路径（默认 项目根/seed/…）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将写入的行数，不落库")
    args = ap.parse_args()

    if args.dry_run:
        p = Path(args.seed or ROOT / "seed" / "business.json")
        business = json.loads(p.read_text(encoding="utf-8"))
        print(f"dry-run：{p}")
        for table in LOAD_ORDER:
            print(f"  {table:<22} {len(business.get(table) or []):>6} 行（将 upsert）")
        return 0

    load_seed(business_path=Path(args.seed) if args.seed else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())