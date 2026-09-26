"""探针：中国平安（601318）的「归母净资产 / 毛利率 / 营业成本」到底在哪个源哪个字段。

背景：券商 App 上中国平安**有**这三项，但我们的东财取数报"数据源未提供"。
说明不是"没有"，而是**字段名或报表选错了**（或保险走的是另一套报表模板）。
所以这里不猜：把每个候选接口的所有列都 dump 出来，只打印非零值，
再按关键词（EQUITY / COST / MARGIN / 股东权益 / 成本）搜。

覆盖四条路：
  A. 证券 API 四个 reportName，columns=ALL
  B. 老版 F10 Ajax 接口（ZCFZBAjaxNew / LRBAjaxNew / XJLLBAjaxNew / KeyIndicatorAjaxNew）
     —— 很多爬虫走这条，可能是保险专属报表模板
  C. 数据中心三大报表，columns=ALL（已知是"业务口径简表"，复核一遍）
  D. 主要指标接口的全部列（找有没有未被我们登记的权益字段）

用法：python scripts/probe_insurance_fields.py [code]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, net  # noqa: E402

CODE = sys.argv[1] if len(sys.argv) > 1 else "601318"
SECUCODE = f"{CODE}.SH" if CODE.startswith(("6", "9")) else f"{CODE}.SZ"
PERIOD = "2024-12-31"

# 关键词 → 我们要找的三个概念（大小写不敏感）
WANT = {
    "归母净资产": ["PARENT_EQUITY", "TOTAL_PARENT_EQUITY", "EQUITY_PARENT",
                   "SHAREHOLDER_EQUITY", "ATTR_PARENT", "归属母", "SE_PARENT"],
    "营业成本": ["OPERATE_COST", "OPERATING_COST", "COST_OF_SALES", "营业成本",
                 "TOTAL_OPERATE_COST", "OPERATE_EXPENSE"],
    "毛利率": ["XSMLL", "GROSS", "GROSSPROFIT", "MARGIN"],
}


def hit(label: str, key: str) -> bool:
    k = key.upper()
    return any(w.upper() in k for w in WANT[label])


def scan(name: str, rows: list[dict], label_hint: str = "") -> dict:
    """打印：总行数 + 全部非空列名 + 目标概念命中情况。"""
    print(f"\n--- {name}{('  [' + label_hint + ']') if label_hint else ''}")
    if not rows:
        print("  （无数据）")
        return {}
    cols = sorted(rows[0].keys())
    print(f"  列数 {len(cols)}；行数 {len(rows)}")

    target = next((r for r in rows if str(r.get("REPORT_DATE", "")).startswith(PERIOD)), None)
    if target is None:
        target = rows[0]
    print(f"  取样本期 {target.get('REPORT_DATE')}")
    nonnull = {k: v for k, v in target.items() if v not in (None, "", 0)}
    print(f"  非空字段 {len(nonnull)} 个")

    found: dict[str, list[str]] = {}
    for label in WANT:
        hits = [k for k in nonnull if hit(label, k)]
        found[label] = hits
        if hits:
            print(f"  ★ 命中「{label}」：")
            for k in hits:
                print(f"      {k} = {nonnull[k]}")
        else:
            print(f"  ✗ 未命中「{label}」")
    return found


def main() -> int:
    SEC = config.EASTMONEY_SECURITIES_API
    DC = config.EASTMONEY_DATACENTER_API

    print("=" * 78)
    print(f"目标公司 {CODE}（{SECUCODE}），样本期 {PERIOD}")
    print("=" * 78)

    print("\n########## A. 证券 API（columns=ALL）##########")
    for rn in ("RPT_F10_FINANCE_GBALANCE", "RPT_F10_FINANCE_GINCOME",
               "RPT_F10_FINANCE_GCASHFLOW", "RPT_F10_FINANCE_MAINFINADATA"):
        params = {"reportName": rn, "columns": "ALL", "pageSize": 8, "pageNumber": 1,
                  "filter": f'(SECUCODE="{SECUCODE}")',
                  "sortColumns": "REPORT_DATE", "sortTypes": -1}
        try:
            d = net.get_json(SEC, params=params)
            ok = (d or {}).get("success")
            msg = (d or {}).get("message")
            rows = ((d or {}).get("result") or {}).get("data") or []
            if not ok:
                print(f"\n--- {rn}\n  !! success={ok} message={msg!r}")
                continue
            scan(rn, rows)
        except Exception as e:
            print(f"\n--- {rn}\n  EXC {type(e).__name__}: {e}")

    print("\n########## B. 老版 F10 Ajax 接口 ##########")
    base = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
    ajax = {
        "资产负债表": f"{base}/ZCFZBAjaxNew?type=0&code={SECUCODE.replace('.', '')}",
        "利润表": f"{base}/LRBAjaxNew?type=0&code={SECUCODE.replace('.', '')}",
        "现金流量表": f"{base}/XJLLBAjaxNew?type=0&code={SECUCODE.replace('.', '')}",
        "主要指标": f"{base}/KeyIndicatorAjaxNew?type=0&code={SECUCODE.replace('.', '')}",
    }
    for label, url in ajax.items():
        try:
            # 老接口要带 Referer，否则可能 403
            d = net.get_json(url, referer="https://emweb.securities.eastmoney.com/")
        except Exception as e:
            print(f"\n--- {label}\n  EXC {type(e).__name__}: {e}")
            continue
        rows = d if isinstance(d, list) else ((d or {}).get("data") or [])
        if not isinstance(rows, list):
            print(f"\n--- {label}\n  返回结构不是列表：{type(rows).__name__}；"
                  f"原始前 200 字：{json.dumps(d, ensure_ascii=False)[:200]}")
            continue
        scan(label, [r for r in rows if isinstance(r, dict)])

    print("\n########## C. 数据中心三大报表（columns=ALL）##########")
    for key in ("dc_balance", "dc_income", "dc_cashflow"):
        rn = config.DATA_SOURCES[key]["report_name"]
        params = {"reportName": rn, "columns": "ALL", "pageSize": 8, "pageNumber": 1,
                  "filter": f'(SECURITY_CODE="{CODE}")',
                  "sortColumns": "REPORT_DATE", "sortTypes": -1}
        try:
            d = net.get_json(DC, params=params)
            rows = ((d or {}).get("result") or {}).get("data") or []
            scan(rn, rows, key)
        except Exception as e:
            print(f"\n--- {rn}\n  EXC {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
