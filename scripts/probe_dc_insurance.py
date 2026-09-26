"""探针：数据中心批量报表对保险股（601318）到底返回什么。

背景：F10 资产负债表/现金流量表对保险公司整表返回空，改用数据中心批量报表兜底；
但实测 601318 仍未命中 dc_*，需要确认是「接口不返回」还是「filter 写法不对」。
"""
from src import config, net


def probe(api_url: str, report_name: str, code: str, filters: list[str],
          columns: str = "ALL") -> None:
    print(f"\n=== {report_name}  filter={filters}")
    params = {"reportName": report_name, "columns": columns,
              "pageSize": 10, "pageNumber": 1,
              "sortColumns": "REPORT_DATE", "sortTypes": -1}
    for i, flt in enumerate(filters):
        p = dict(params)
        if flt:
            p["filter"] = flt
        try:
            d = net.get_json(api_url, params=p)
        except Exception as e:
            print(f"  [{i}] EXC {type(e).__name__}: {e}")
            continue
        ok = (d or {}).get("success")
        msg = (d or {}).get("message")
        rows = ((d or {}).get("result") or {}).get("data") or []
        print(f"  [{i}] flt={flt!r} success={ok} msg={msg!r} rows={len(rows)}")
        if rows:
            print(f"       cols({len(rows[0])}): {sorted(rows[0])[:45]}")
            for r in rows[:5]:
                print("       ", r.get("REPORT_DATE"), r.get("SECURITY_NAME_ABBR"),
                      r.get("SECURITY_CODE"))


if __name__ == "__main__":
    code = "601318"
    DC = config.EASTMONEY_DATACENTER_API
    for key in ["dc_balance", "dc_income", "dc_cashflow"]:
        spec = config.DATA_SOURCES[key]
        probe(DC, spec["report_name"], code, [
            f'(SECURITY_CODE="{code}")',
            f'(SECURITY_CODE="{code}")(REPORT_DATE=\'2024-12-31\')',
            "",  # 不带 filter，看接口本身通不通
        ])
