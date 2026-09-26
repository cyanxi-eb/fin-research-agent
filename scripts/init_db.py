"""建库脚本 —— 建表 + 灌演示数据（Step 2 的一键入口）。

分工说明（为什么不把逻辑都写这儿）：
真正的建表 SQL 在 `src/db.py`（双后端 DDL），取数清洗在 `src/ingest/fetch_eastmoney.py`。
本脚本只做**编排**：按顺序调用它们，并打印一份人能看懂的落地统计。
把 SQL 抄进脚本 = 建表逻辑出现第二份事实来源，迟早和 db.py 走偏。

用法：
    python scripts/init_db.py                 # 建表 + 同步公司/年报清单（不联网）
    python scripts/init_db.py --fetch         # 再拉一次东财三大报表（联网，约 30~60s）
    python scripts/init_db.py --reset         # 先清库再建（sqlite 直接删库文件）
    python scripts/init_db.py --backend mysql # 显式指定后端（等价于环境变量 FA_DB_BACKEND）

注意：`--reset` 会**删掉整个 sqlite 库文件**（含审计日志、评估集），仅开发期使用。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402


def reset_sqlite() -> None:
    """删掉 sqlite 库文件（MySQL 走 TRUNCATE，见 db.reset_business_tables）。"""
    if db.IS_MYSQL:
        db.reset_business_tables()
        print("已 TRUNCATE 全部业务表（MySQL）")
        return
    if config.DB_PATH.exists():
        size = config.DB_PATH.stat().st_size
        config.DB_PATH.unlink()
        print(f"已删除 sqlite 库文件（{size / 1024:.0f} KB）：{config.DB_PATH}")
    else:
        print(f"sqlite 库文件不存在，跳过：{config.DB_PATH}")


def main() -> int:
    args = sys.argv[1:]
    do_reset = "--reset" in args
    do_fetch = "--fetch" in args
    if "--backend" in args:
        # 只作为提示：后端在 config 导入时就已解析，运行期改不了（改 env 或 db_keys.local.json）
        want = args[args.index("--backend") + 1]
        if want != config.DB_BACKEND:
            print(f"⚠️ --backend {want} 与当前生效后端 {config.DB_BACKEND} 不一致；"
                  f"请在启动前设置 FA_DB_BACKEND，或改 data/db_keys.local.json")
            return 2

    print(f"后端 {config.DB_BACKEND}  库 {config.DB_PATH if not db.IS_MYSQL else config.MYSQL}")
    if do_reset:
        reset_sqlite()

    db.init_schema()
    print("建表完成（幂等）")

    # 1) 公司清单 + 年报清单（本地事实，不联网）
    from src.ingest.fetch_eastmoney import sync_companies, sync_reports_from_parsed

    n_co = sync_companies()
    n_rep = sync_reports_from_parsed()
    print(f"同步 companies {n_co} 家、reports {n_rep} 份（来自 config/watchlist.yaml 与 data/parsed）")

    # 2) 结构化财务数据（联网，可选）
    if do_fetch:
        from src.ingest.fetch_eastmoney import fetch_watchlist

        stats = fetch_watchlist()
        print(f"\n东财取数 {len(stats)} 家：")
        for s in stats:
            flag = "⚠" if s.get("missing") else "✓"
            print(f"  {flag} {s['code']} {s.get('name', ''):<10} "
                  f"{s['periods']} 期 / {s['rows']} 个指标值  源={','.join(s['sources_used'])}")
            if s.get("missing"):
                print(f"      全源缺失（该指标对此行业不适用）：{s['missing']}")

    # 3) 落地统计
    print("\n=== 库内现状 ===")
    with db.get_conn() as conn:
        for t in db.ALL_TABLES:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
            n = row["n"] if isinstance(row, dict) else row[0]
            print(f"  {t:<22} {n:>6} 行")
        cur = conn.execute(
            "SELECT indicator, COUNT(*) AS n FROM financial_indicators "
            "GROUP BY indicator ORDER BY n DESC, indicator")
        per = cur.fetchall()
    if per:
        got = [f"{d['indicator']}({d['n']})" if isinstance(d, dict)
               else f"{d[0]}({d[1]})" for d in per]
        print(f"\n按指标覆盖（条数）：{', '.join(got)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
