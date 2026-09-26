"""探针 7：dump 新浪资产负债表/利润表的**全部项目名**（制造业 vs 保险），用于精确配字段。

为什么必须 dump 而不是猜：新浪的项目名是中文，且不同行业的报表模板项目名不同
（保险利润表是「营业收入 / 营业支出」，制造业是「营业总收入 / 营业成本 / 营业总成本」）。
拿错名字的后果是 `raw.get(field)` 返回 None —— 静默缺数据，不会报错。

同时对同一家公司打印「东财已入库值 vs 新浪值」的对照，验证字段映射正确。

用法：python scripts/probe_sina_items.py [600519] [601318]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import db  # noqa: E402
from src.ingest import fetch_sina  # noqa: E402

CODES = sys.argv[1:] or ["600519", "601318", "000858"]
PERIOD = "2024-12-31"


def show(code: str) -> None:
    print("\n" + "=" * 78)
    print(f"{code}  期次 {PERIOD}")
    print("=" * 78)

    want = {
        "营业总收入", "营业收入", "营业成本", "营业总成本", "营业支出",
        "归母净利润", "净利润", "总资产", "总负债", "归母净资产",
        "所有者权益合计", "经营活动现金流净额",
    }

    for source_key in ("sina_balance", "sina_income"):
        rows = fetch_sina.fetch_source(source_key, code, periods=8,
                                       report_type="年报")
        print(f"\n--- {source_key}：{len(rows)} 期")
        if not rows:
            continue
        row = next((r for r in rows if r["REPORT_DATE"] == PERIOD), None)
        if row is None:
            print(f"  该源无 {PERIOD}（可用期：{[r['REPORT_DATE'] for r in rows][:5]}）")
            continue
        names = [k for k in row if k != "REPORT_DATE"]
        print(f"  项目数 {len(names)}")
        # 与我们口径表里的指标名做交集提示
        hit = [n for n in names if any(n in w or w in n for w in want)]
        print(f"  ★ 可能与口径表相关的项目（{len(hit)} 个）：")
        for n in hit:
            v = row[n]
            if isinstance(v, (int, float)):
                print(f"      {n} = {v/1e8:,.2f} 亿")
            else:
                print(f"      {n} = {v}")

    # 与库里东财值对照
    print(f"\n--- 库里（东财）{PERIOD} 的值")
    with db.get_conn() as conn:
        cur = conn.execute(
            "SELECT indicator, value, unit, source_table, source_field "
            "FROM financial_indicators WHERE code=? AND period=? ORDER BY indicator",
            (code, PERIOD))
        for r in cur.fetchall():
            d = dict(r) if isinstance(r, dict) else {k: r[k] for k in r.keys()}
            v = d["value"]
            disp = f"{v/1e8:,.2f} 亿" if d["unit"] == "元" else f"{v:,.2f}{d['unit']}"
            print(f"      {d['indicator']:<12} {disp:>16}  <- {d['source_table']}.{d['source_field']}")


def main() -> int:
    for code in CODES:
        show(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
