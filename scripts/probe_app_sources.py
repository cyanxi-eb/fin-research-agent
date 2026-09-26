"""探针：券商 App 上看到的「营业成本 / 毛利率 / 归母净资产」到底映射到哪个字段。

为什么需要这个探针：
用户看到券商 App 上中国平安**有**这三项，而我们的取数报"数据源未提供"。
结论只有两种可能：(a) 我们选错了字段/报表；(b) 数据客观不存在，App 是用别的科目
**换了个标签**显示。两者对系统的含义完全不同 —— (a) 要改口径表，(b) 要在回答里
显式说明"该科目在保险业不存在，对应科目是 X"，否则用户会认为我们在糊弄。

所以这里把每个源的相关字段**全部打印出来**（含值），用数据而不是印象来判定。

覆盖的源（按券商 App 的常见取数来源）：
  - 东财 F10 证券接口 RPT_F10_FINANCE_GINCOME / GBALANCE / MAINFINADATA（columns=ALL）
  - 东财数据中心 RPT_DMSK_FN_INCOME / BALANCE
  - 新浪移动端财报 lrb（利润表）/ fzb（资产负债表）
  - 同花顺 basic 财务概览（flashData 是**二次转义的 JSON 字符串**，必须 json.loads 两次）

用法：
    python scripts/probe_app_sources.py              # 默认 601318（保险）
    python scripts/probe_app_sources.py 600519       # 对照制造业
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, net  # noqa: E402
from src.ingest import fetch_sina  # noqa: E402

CODE = sys.argv[1] if len(sys.argv) > 1 else "601318"
PERIOD = "2024-12-31"
SECUCODE = f"{CODE}.SH" if CODE.startswith(("6", "9")) else f"{CODE}.SZ"

# 只打「跟这三个概念有关」的字段，避免几百列刷屏
KEYWORDS = ("COST", "EXPENSE", "PROFIT", "INCOME", "EQUITY", "MARGIN", "XSMLL",
            "成本", "支出", "利润", "收入", "权益", "毛利", "净资产")


def _relevant(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        ku = str(k).upper()
        if v in (None, "", 0):
            continue
        if any(w in ku for w in KEYWORDS):
            out[k] = v
    return out


def _fmt(v) -> str:
    if isinstance(v, (int, float)) and abs(v) > 1e5:
        return f"{v / 1e8:,.2f} 亿"
    return str(v)


def scan_em(name: str, api: str, report_name: str, filt: str, period: str) -> None:
    params = {"reportName": report_name, "columns": "ALL", "pageSize": 8, "pageNumber": 1,
              "filter": filt, "sortColumns": "REPORT_DATE", "sortTypes": -1}
    print(f"\n--- {name}  ({report_name})")
    try:
        d = net.get_json(api, params=params)
    except Exception as e:                                   # noqa: BLE001
        print(f"    EXC {type(e).__name__}: {str(e)[:110]}")
        return
    rows = ((d or {}).get("result") or {}).get("data") or []
    print(f"    success={(d or {}).get('success')} message={(d or {}).get('message')!r} "
          f"rows={len(rows)}")
    if not rows:
        return
    tgt = next((r for r in rows if str(r.get("REPORT_DATE", "")).startswith(period)), rows[0])
    rel = _relevant(tgt)
    print(f"    样本期 {tgt.get('REPORT_DATE')}；相关字段 {len(rel)} 个")
    for k, v in sorted(rel.items()):
        print(f"      {k:<30} = {_fmt(v)}")


def scan_sina() -> None:
    for key in ("sina_income", "sina_balance"):
        print(f"\n--- {key}（新浪）")
        try:
            rows = fetch_sina.fetch_source(key, CODE, periods=8, report_type="年报")
        except Exception as e:                               # noqa: BLE001
            print(f"    EXC {type(e).__name__}: {str(e)[:110]}")
            continue
        tgt = next((r for r in rows if r["REPORT_DATE"] == PERIOD), None)
        if tgt is None:
            print(f"    无 {PERIOD}")
            continue
        rel = _relevant(tgt)
        print(f"    相关字段 {len(rel)} 个")
        for k, v in sorted(rel.items()):
            print(f"      {k:<30} = {_fmt(v)}")


def scan_ths() -> None:
    """同花顺 basic 财务概览。

    坑：`flashData` 是**二次转义的 JSON 字符串**（值本身又是一段 JSON 文本），
    直接对整体做中文关键词匹配会全部落空 —— 看上去像"这个源没有中文科目名"。
    """
    print("\n--- 同花顺 basic main（券商 App 常用）")
    try:
        d = net.get_json(f"https://basic.10jqka.com.cn/api/stock/finance/{CODE}_main.json",
                         referer="https://basic.10jqka.com.cn/")
    except Exception as e:                                   # noqa: BLE001
        print(f"    EXC {type(e).__name__}: {str(e)[:110]}")
        return
    fd = d.get("flashData")
    if isinstance(fd, str):
        fd = json.loads(fd)
    title = (fd or {}).get("title") or []
    names = [t[0] for t in title if isinstance(t, list) and t and isinstance(t[0], str)]
    print(f"    科目数 {len(names)}")
    hit = [n for n in names if any(w in n for w in ("成本", "支出", "毛利", "收入", "权益"))]
    print(f"    ★ 收入/成本/权益相关科目：{hit}")
    if not hit:
        print("    （该源没有这些科目名）")


def main() -> int:
    print("=" * 78)
    print(f"目标 {CODE}（{SECUCODE}），样本期 {PERIOD}；单位统一换算成「亿」显示")
    print("=" * 78)

    SEC, DC = config.EASTMONEY_SECURITIES_API, config.EASTMONEY_DATACENTER_API
    scan_em("东财 F10 利润表", SEC, "RPT_F10_FINANCE_GINCOME", f'(SECUCODE="{SECUCODE}")', PERIOD)
    scan_em("东财 F10 资产负债表", SEC, "RPT_F10_FINANCE_GBALANCE", f'(SECUCODE="{SECUCODE}")', PERIOD)
    scan_em("东财 F10 主要指标", SEC, "RPT_F10_FINANCE_MAINFINADATA", f'(SECUCODE="{SECUCODE}")', PERIOD)
    scan_em("东财数据中心 利润表", DC, "RPT_DMSK_FN_INCOME", f'(SECURITY_CODE="{CODE}")', PERIOD)
    scan_em("东财数据中心 资产负债表", DC, "RPT_DMSK_FN_BALANCE", f'(SECURITY_CODE="{CODE}")', PERIOD)
    scan_sina()
    scan_ths()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
