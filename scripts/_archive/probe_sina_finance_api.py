"""探针 3：新浪财经的**结构化 JSON** 接口（优先于 HTML 表格）。

HTML 表格虽然能正则解，但表头/合并单元格一旦改版就会静默出错。
新浪移动端有一套 JSON 接口（`CompanyFinanceService.getFinanceReport2022`），
返回「项目名 + 值」的结构化数组，比解 HTML 稳得多。这里验证它到底能不能用、
字段名与单位是什么，以及 601318 的归母净资产/营业成本/毛利率是否都在里面。

用法：python scripts/probe_sina_finance_api.py [code]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from src import config  # noqa: E402

CODE = sys.argv[1] if len(sys.argv) > 1 else "601318"
PAPER = ("sh" if CODE.startswith(("6", "9")) else "sz") + CODE
UA = config.FETCH_UA

# 新浪这套接口的三张表代号
SOURCES = {"zcfz": "资产负债表", "lrb": "利润表", "xjll": "现金流量表"}

# 我们要找的概念 → 项目名关键词
WANT = {
    "归母净资产": ["归属于母公司", "母公司股东权益", "归属母公司"],
    "营业成本": ["营业成本", "营业总成本", "营业支出"],
    "毛利率": ["毛利率"],
}


def fetch(source: str, year: int = 2024, num: int = 5) -> dict | None:
    url = ("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService"
           "/getFinanceReport2022")
    params = {"paperCode": PAPER, "source": source, "type": "0",
              "page": "1", "num": str(num), "year": str(year)}
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
         "Referer": f"https://finance.sina.com.cn/realstock/company/{PAPER}/nc.shtml"}
    r = requests.get(url, params=params, headers=h, timeout=config.FETCH_TIMEOUT)
    print(f"\n===== {SOURCES.get(source, source)}  HTTP {r.status_code}  "
          f"{r.headers.get('Content-Type','')}  长度 {len(r.content)}")
    try:
        return r.json()
    except Exception as e:
        print(f"  非 JSON：{type(e).__name__}: {e}；前 200 字 {r.text[:200]!r}")
        return None


def walk(obj, path="") -> list[tuple[str, object]]:
    """把嵌套结构摊平成 (路径, 叶子值) 列表，便于找"项目名→值"的配对。"""
    out: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += walk(v, f"{path}[{i}]")
    else:
        out.append((path, obj))
    return out


def main() -> int:
    print("=" * 78)
    print(f"目标 {CODE}（paperCode={PAPER}）  新浪移动端财报接口")
    print("=" * 78)

    for source, label in SOURCES.items():
        data = fetch(source)
        if data is None:
            continue
        result = (data.get("result") or {})
        print(f"  result 键：{sorted(result)}")
        flat = walk(result)
        print(f"  叶子数 {len(flat)}")
        # 打印前若干条，看清"项目名/值"是怎么组织的
        print("  前 12 条摊平样本：")
        for p, v in flat[:12]:
            s = str(v)
            print(f"    {p} = {s[:60]}")
        # 找目标概念
        for label_, kws in WANT.items():
            for p, v in flat:
                if any(kw in str(v) for kw in kws):
                    print(f"  ★ [{label_}] {p} = {v}")
                    break

    # 财务指标/摘要（毛利率可能在这里）
    print("\n===== 财务指标摘要接口 =====")
    for api in ("getFinanceSummary", "getFinanceIndicator"):
        url = f"https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService/{api}"
        try:
            r = requests.get(url, params={"paperCode": PAPER},
                             headers={"User-Agent": UA,
                                      "Referer": f"https://finance.sina.com.cn/realstock/company/{PAPER}/nc.shtml"},
                             timeout=config.FETCH_TIMEOUT)
            print(f"\n--- {api}  HTTP {r.status_code}  {r.headers.get('Content-Type','')}")
            print(f"  前 300 字：{r.text[:300]!r}")
        except Exception as e:
            print(f"\n--- {api}\n  EXC {type(e).__name__}: {e}")

    # 兜底：把 zcfz 的原始 JSON 存下来，便于人工核对字段名
    zcfz = fetch("zcfz")
    if zcfz:
        out = config.DATA_DIR / f"sina_zcfz_{CODE}.json"
        out.write_text(json.dumps(zcfz, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n原始 zcfz JSON 已存 {out}（供人工核对字段名）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
