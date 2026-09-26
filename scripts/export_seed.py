"""导出种子 —— 把当前语料与业务数据打包成 `seed/`，供容器/VM 复用同一份交付物。

为什么要"导出成种子"而不是让部署机重新取数：
取数要联网打巨潮/东财/新浪（有反爬、会限流、字段还可能变），embedding 走 API 要花钱。
把**已经跑通并核对过**的语料（解析文本 / 两个 BM25 索引 / 向量四件套 / 法规原文）与
业务表快照固化成 `seed/`，部署时就是一次本地拷贝 + 一次 upsert，**结果可复现**。

产出：
    seed/data/…        语料镜像（parsed / index / vector / regulation 四份，逐字节拷贝）
    seed/business.json 四张业务表的行（**后端中立**的纯 JSON，SQLite/MySQL 通用）

用法：
    python scripts/export_seed.py                 # 读当前后端（默认 sqlite）→ 写 seed/
    python scripts/export_seed.py --out seed      # 指定输出目录

⚠️ 只导出上面**四个语料子目录 + 四张业务表**。`data/*.local.json`（口令）直接躺在
`data/` 根下、不在被拷贝的子目录里，所以天然不会被带进 `seed/`；也不要顺手把
`data/db/` 或 `data/raw/` 加进来 —— 前者是运行时库、后者是上百 MB 的 PDF 原件。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402

# 要镜像的语料子目录。index 含 bm25.pkl 与 bm25_regulation.pkl；vector 是四件套。
SEED_DATA_SUBDIRS = ("parsed", "index", "vector", "regulation")

# 表 → 显式列清单（不 `SELECT *`）。
# 为什么写死列名：让 business.json **后端中立且稳定** —— MySQL 的 `SELECT *` 列序、
# 未来新增的生成列都会混进来，令 SQLite 与 MySQL 导出的同一份数据 hash 不同。
# 列序也与 `db.upsert_*_sql()` 的占位符顺序对齐，load 时可直接按列名取值。
TABLES: dict[str, tuple[str, ...]] = {
    "companies": ("code", "name", "market", "industry", "org_id", "updated_at"),
    "reports": ("code", "year", "report_type", "title", "url", "pdf_path", "size_kb",
                "page_count", "empty_pages", "section_mode", "parsed_at"),
    "financial_indicators": ("code", "period", "report_type", "indicator", "value",
                             "unit", "source_table", "source_field", "fetched_at"),
    "golden_qa": ("qa_id", "question", "expected_answer", "expected_citations",
                  "scene", "created_at"),
}

# 各表的稳定排序键（= 主键）。固定顺序让 business.json 可 diff、可复现。
ORDER_BY: dict[str, str] = {
    "companies": "code",
    "reports": "code, year, report_type",
    "financial_indicators": "code, period, report_type, indicator",
    "golden_qa": "qa_id",
}


def _mirror_subdirs(data_dir: Path, dst_data: Path) -> list[str]:
    """把语料子目录镜像到 seed/data/（先清后拷，保证与源**逐字节一致**、无残留）。

    先 `rmtree` 再 `copytree` 而不是 `dirs_exist_ok` 覆盖：源里删掉的旧文件若残留在
    seed 里，会让"种子"和"当前语料"静默分叉 —— 部署机拿到的是一份混合体。
    """
    copied = []
    for name in SEED_DATA_SUBDIRS:
        src = data_dir / name
        if not src.exists():
            print(f"⚠️ 跳过不存在的语料目录：{src}")
            continue
        dst = dst_data / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        copied.append(name)
    return copied


def _dump_business() -> dict[str, list[dict]]:
    """读当前后端（默认 sqlite）的四张表，返回 {表名: [行 dict, …]}。"""
    out: dict[str, list[dict]] = {}
    with db.get_conn() as conn:
        for table, cols in TABLES.items():
            sel = ", ".join(cols)
            cur = conn.execute(f"SELECT {sel} FROM {table} ORDER BY {ORDER_BY[table]}")
            out[table] = [dict(row) for row in cur.fetchall()]
    return out


def export_seed(seed_dir: Path | None = None, data_dir: Path | None = None) -> dict[str, int]:
    """执行导出，返回各表行数（供测试与调用方复核）。"""
    seed = Path(seed_dir or ROOT / "seed")
    data = Path(data_dir or config.DATA_DIR)
    dst_data = seed / "data"
    dst_data.mkdir(parents=True, exist_ok=True)

    copied = _mirror_subdirs(data, dst_data)
    business = _dump_business()

    biz_path = seed / "business.json"
    biz_path.write_text(
        json.dumps(business, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    counts = {t: len(rows) for t, rows in business.items()}
    print(f"后端 {config.DB_BACKEND}；语料镜像 {copied} → {dst_data}")
    print(f"已写 {biz_path}（后端中立，SQLite/MySQL 通用）")
    print("=== seed 各表行数 ===")
    for table in TABLES:
        print(f"  {table:<22} {counts[table]:>6} 行")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 seed/（语料镜像 + business.json）")
    ap.add_argument("--out", default=None, help="输出目录（默认 项目根/seed）")
    args = ap.parse_args()
    export_seed(seed_dir=Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())