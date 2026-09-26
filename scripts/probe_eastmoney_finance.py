"""东财财务数据接口探测 —— Step 2 的取数依据。

为什么要单独探：`config.INDICATORS` 里的 `source_field` 是**取数的唯一事实来源**，
写错一个字段名，取回来的就是 None 或错指标，而这类错误在数据里**看起来很正常**
（有数字、有报告期），只有跟年报原文对账才会发现。所以字段名一律实测，不凭记忆写。

本脚本把三大报表（资产负债表/利润表/现金流量表）的候选接口逐个试，
打印「哪个通了、返回了多少期、有哪些字段」，供人工比对后填进 config。

用法：
    python scripts/probe_eastmoney_finance.py [代码]        # 默认 600519
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, net  # noqa: E402

CODE = sys.argv[1] if len(sys.argv) > 1 else "600519"
SECUCODE = f"{CODE}.{'SH' if CODE.startswith(('6', '9')) else 'SZ'}"

# ---- 候选一：东财数据中心（datacenter-web）批量报表 ----
# 这套是「数据中心 → 年报」页面的接口，一次给多家公司的同一张表
DC_CANDIDATES: list[tuple[str, str, dict]] = [
    ("资产负债表", "RPT_DMSK_FN_BALANCE", {"filter": f'(SECURITY_CODE="{CODE}")'}),
    ("利润表", "RPT_DMSK_FN_INCOME", {"filter": f'(SECURITY_CODE="{CODE}")'}),
    ("现金流量表", "RPT_DMSK_FN_CASHFLOW", {"filter": f'(SECURITY_CODE="{CODE}")'}),
]

# ---- 候选二：东财 F10 证券页（datacenter.eastmoney.com/securities）----
F10_CANDIDATES: list[tuple[str, str]] = [
    ("资产负债表", "RPT_F10_FINANCE_GBALANCE"),
    ("利润表", "RPT_F10_FINANCE_GINCOME"),
    ("现金流量表", "RPT_F10_FINANCE_GCASHFLOW"),
]

# ---- 候选三：老版 F10 Ajax（emweb）----
EMWEB_CANDIDATES: list[tuple[str, str]] = [
    ("资产负债表", "zcfzbAjaxNew"),
    ("利润表", "lrbAjaxNew"),
    ("现金流量表", "xjllbAjaxNew"),
]

# 我们真正需要落库的指标 → 期望字段名（用来核对接口是否够用）
NEEDED = [
    "TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT", "OPERATE_COST", "TOTAL_ASSETS",
    "TOTAL_LIABILITIES", "TOTAL_PARENT_EQUITY", "TOTAL_EQUITY",
    "NETCASH_OPERATE", "DEDUCT_PARENT_NETPROFIT", "WEIGHTAVG_ROE",
]


def _report(label: str, rows: list | None, extra: str = "") -> None:
    if not rows:
        print(f"    ✗ {label:<14} 无数据 {extra}")
        return
    first = rows[0]
    keys = sorted(first.keys()) if isinstance(first, dict) else []
    hit = [k for k in NEEDED if k in keys]
    print(f"    ✓ {label:<14} {len(rows)} 期  {extra}")
    print(f"        报告期样例: {first.get('REPORTDATE') or first.get('REPORT_DATE')}")
    print(f"        命中需要的字段 {len(hit)}/{len(NEEDED)}: {hit}")
    if len(hit) < len(NEEDED):
        missing = [k for k in NEEDED if k not in keys]
        print(f"        未命中: {missing}")
    print(f"        全部字段({len(keys)}): {keys}")


def probe_datacenter_web() -> None:
    print("[候选一] datacenter-web.eastmoney.com/api/data/v1/get（数据中心批量报表）")
    for label, report_name, extra_params in DC_CANDIDATES:
        params = {
            "reportName": report_name,
            "columns": "ALL",
            "pageSize": 5, "pageNumber": 1,
            "sortColumns": "REPORT_DATE", "sortTypes": -1,
            **extra_params,
        }
        try:
            data = net.get_json("https://datacenter-web.eastmoney.com/api/data/v1/get",
                                params=params)
        except Exception as e:
            print(f"    ✗ {label:<14} {type(e).__name__}: {e}")
            continue
        rows = ((data or {}).get("result") or {}).get("data") or []
        ok = (data or {}).get("success")
        _report(label, rows, f"success={ok} reportName={report_name}")


def probe_f10() -> None:
    print("\n[候选二] datacenter.eastmoney.com/securities/api/data/v1/get（F10 证券页）")
    for label, report_name in F10_CANDIDATES:
        params = {
            "reportName": report_name,
            "columns": "ALL",
            "pageSize": 5, "pageNumber": 1,
            "filter": f'(SECUCODE="{SECUCODE}")',
            "sortColumns": "REPORT_DATE", "sortTypes": -1,
        }
        try:
            data = net.get_json("https://datacenter.eastmoney.com/securities/api/data/v1/get",
                                params=params)
        except Exception as e:
            print(f"    ✗ {label:<14} {type(e).__name__}: {e}")
            continue
        rows = ((data or {}).get("result") or {}).get("data") or []
        _report(label, rows, f"reportName={report_name}")


def probe_emweb() -> None:
    print("\n[候选三] emweb.securities.eastmoney.com（老版 F10 Ajax）")
    for label, endpoint in EMWEB_CANDIDATES:
        url = f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/{endpoint}"
        params = {
            "companyType": 4, "reportDateType": 0, "reportType": 1,
            "dates": "2024-12-31,2023-12-31", "code": SECUCODE,
        }
        try:
            data = net.get_json(url, params=params)
        except Exception as e:
            print(f"    ✗ {label:<14} {type(e).__name__}: {e}")
            continue
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        _report(label, rows if isinstance(rows, list) else None,
                f"{endpoint} 顶层键={list(data.keys())[:6] if isinstance(data, dict) else type(data).__name__}")


def probe_industry_compare_row() -> None:
    """顺手确认：一条记录里是否同时含「营收/归母净利/总资产/ROE」——若够用，
    最低成本的方案就是只依赖这一张表。"""
    print("\n[参考] RPT_LICO_FN_CPD（业绩报表，已实测可用）字段盘点")
    data = net.get_json(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_LICO_FN_CPD", "columns": "ALL", "pageSize": 3,
                "filter": f'(SECURITY_CODE="{CODE}")',
                "sortColumns": "REPORTDATE", "sortTypes": -1})
    rows = ((data or {}).get("result") or {}).get("data") or []
    if not rows:
        print("    ✗ 无数据")
        return
    keys = sorted(rows[0].keys())
    print(f"    ✓ {len(rows)} 期，共 {len(keys)} 字段")
    print(f"        样例: {rows[0].get('REPORTDATE','')[:10]} "
          f"营收={rows[0].get('TOTAL_OPERATE_INCOME')} "
          f"归母净利={rows[0].get('PARENT_NETPROFIT')}")
    print(f"        全部字段: {keys}")


def probe_main_indicators() -> None:
    """主要指标接口 —— 毛利率/净利率/ROE 这类比率，若有官方口径就用官方的，
    比自己拿分子分母算更权威（也便于给 ratios.py 做对账）。"""
    print("\n[候选四] 主要指标（RPT_F10_FINANCE_GMAINFINADATA / RPT_F10_FINANCE_MAINFINADATA）")
    wanted = ["WEIGHTAVG_ROE", "GROSS_PROFIT_RATIO", "NET_PROFIT_RATIO",
              "DEBT_ASSET_RATIO", "TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT",
              "BASIC_EPS", "BPS", "TOTALOPERATEREVETZ", "PARENTNETPROFITTZ"]
    for report_name in ("RPT_F10_FINANCE_GMAINFINADATA", "RPT_F10_FINANCE_MAINFINADATA"):
        params = {
            "reportName": report_name, "columns": "ALL",
            "pageSize": 3, "pageNumber": 1,
            "filter": f'(SECUCODE="{SECUCODE}")',
            "sortColumns": "REPORT_DATE", "sortTypes": -1,
        }
        try:
            data = net.get_json("https://datacenter.eastmoney.com/securities/api/data/v1/get",
                                params=params)
        except Exception as e:
            print(f"    ✗ {report_name} {type(e).__name__}: {e}")
            continue
        rows = ((data or {}).get("result") or {}).get("data") or []
        if not rows:
            print(f"    ✗ {report_name} 无数据 success={(data or {}).get('success')}")
            continue
        keys = sorted(rows[0].keys())
        hit = [k for k in wanted if k in keys]
        print(f"    ✓ {report_name}  {len(rows)} 期  命中 {len(hit)}/{len(wanted)}: {hit}")
        print(f"        样例 {rows[0].get('REPORT_DATE', '')[:10]}: "
              + "  ".join(f"{k}={rows[0].get(k)}" for k in hit[:5]))
        print(f"        全部字段({len(keys)}): {keys}")


def probe_multi_period_and_values() -> None:
    """两个必须核实的点：
    1. F10 接口能否**一次拿多期**（否则要按报告期逐个请求，慢且易被限流）；
    2. 取回来的**数值口径**是否与年报一致 —— 字段名对了但取错期/错单位，
       在数据里看起来同样正常，必须人工对账一次。
    """
    print(f"\n[核对] 多期拉取 + 数值合理性（{CODE}）")
    for label, report_name, fields in (
        ("利润表", "RPT_F10_FINANCE_GINCOME",
         "REPORT_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,DEDUCT_PARENT_NETPROFIT,OPERATE_COST"),
        ("资产负债表", "RPT_F10_FINANCE_GBALANCE",
         "REPORT_DATE,TOTAL_ASSETS,TOTAL_LIABILITIES,TOTAL_PARENT_EQUITY,TOTAL_EQUITY"),
        ("现金流量表", "RPT_F10_FINANCE_GCASHFLOW",
         "REPORT_DATE,NETCASH_OPERATE,NETCASH_INVEST,NETCASH_FINANCE"),
    ):
        data = net.get_json(
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
            params={"reportName": report_name, "columns": fields,
                    "pageSize": 8, "pageNumber": 1,
                    "filter": f'(SECUCODE="{SECUCODE}")',
                    "sortColumns": "REPORT_DATE", "sortTypes": -1})
        rows = ((data or {}).get("result") or {}).get("data") or []
        print(f"    {label}：{len(rows)} 期")
        for r in rows[:8]:
            vals = "  ".join(f"{k.split('_')[0][:6]}={r.get(k)}"
                            for k in fields.split(",") if k != "REPORT_DATE")
            print(f"      {str(r.get('REPORT_DATE'))[:10]}  {vals}")


if __name__ == "__main__":
    print(f"探测标的：{CODE}（{SECUCODE}）\n")
    probe_datacenter_web()
    probe_f10()
    probe_emweb()
    probe_main_indicators()
    probe_industry_compare_row()
    probe_multi_period_and_values()
    print("\n结论请填进 src/config.py 的 INDICATORS[*]['source_field']。")
