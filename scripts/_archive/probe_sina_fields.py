"""探针 4：确定「新浪」作为补充源时的**确切字段与取值方式**。

要解决的三个问题：
  1. 利润表 JSON（可用）里，保险口径的成本科目到底叫什么（营业成本？营业支出？营业总成本？）
     以及 2024 年报那一期怎么定位（返回里给了 report_date 列表 + report_list 按日期分组）。
  2. 资产负债表 JSON 返回 data=None —— 是参数问题还是该 source 不支持；换几个变体重试。
  3. 若资产负债表的 JSON 版本不可用，验证 HTML 表格（vFD_BalanceSheet）能否稳定解析出
     「归属于母公司的股东权益合计」，并确认期次与单位（新浪用**万元**）。

用法：python scripts/probe_sina_fields.py [code]
"""
from __future__ import annotations

import json
import re
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
HDR = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
       "Referer": f"https://finance.sina.com.cn/realstock/company/{PAPER}/nc.shtml"}


def get(url: str, params: dict | None = None) -> requests.Response:
    return requests.get(url, params=params, headers=HDR, timeout=config.FETCH_TIMEOUT)


def decode(resp: requests.Response) -> str:
    raw = resp.content
    if b"charset=gbk" in raw[:600].lower() or b"charset=gb2312" in raw[:600].lower():
        return raw.decode("gbk", errors="replace")
    return raw.decode("utf-8", errors="replace")


# ---------- 1. 利润表 JSON：全部项目名 ----------
def dump_lrb_items() -> None:
    url = ("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService"
           "/getFinanceReport2022")
    r = get(url, {"paperCode": PAPER, "source": "lrb", "type": "0",
                  "page": "1", "num": "5", "year": "2024"})
    data = (r.json().get("result") or {}).get("data") or {}
    rl = data.get("report_list") or {}
    print(f"\n########## 1. 利润表 JSON：report_list 含 {len(rl)} 期")
    # 找 2024 年报那一期
    target_key = None
    for dt in (data.get("report_date") or []):
        if str(dt.get("date_value")) == "20241231":
            target_key = "20241231"
    key = target_key or sorted(rl)[0]
    print(f"  取期 {key}")
    items = ((rl.get(key) or {}).get("data")) or []
    print(f"  项目数 {len(items)}")
    for it in items:
        t = str(it.get("item_title", ""))
        v = it.get("item_value")
        if re.search(r"营业|成本|支出|收入", t):
            print(f"    {t} = {v}")


# ---------- 2. 资产负债表 JSON：参数变体重试 ----------
def retry_zcfz() -> None:
    url = ("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService"
           "/getFinanceReport2022")
    print("\n########## 2. 资产负债表 JSON 参数变体")
    variants = [
        ("source=zcfz&year=2024", {"source": "zcfz", "year": "2024"}),
        ("source=zcfz 无 year", {"source": "zcfz"}),
        ("source=zcfz&type=4", {"source": "zcfz", "type": "4"}),
        ("source=zcfz&num=10", {"source": "zcfz", "num": "10"}),
        ("source=fzb", {"source": "fzb"}),
        ("source=balance", {"source": "balance"}),
    ]
    for label, extra in variants:
        p = {"paperCode": PAPER, "type": "0", "page": "1", "num": "5"}
        p.update(extra)
        try:
            r = get(url, p)
            j = r.json()
            res = (j.get("result") or {})
            data = res.get("data")
            code = (res.get("status") or {}).get("code")
            if data:
                rl = data.get("report_list") or {}
                print(f"  ✓ {label}: status={code} 期数={len(rl)} 键={sorted(rl)[:3]}")
            else:
                print(f"  ✗ {label}: status={code} data=None  msg={(res.get('status') or {}).get('msg')}")
        except Exception as e:
            print(f"  ✗ {label}: EXC {type(e).__name__}: {e}")


# ---------- 3. HTML 资产负债表解析 ----------
def parse_html_balance(year: int = 2024) -> None:
    url = (f"https://money.finance.sina.com.cn/corp/go.php/vFD_BalanceSheet"
           f"/stockid/{CODE}/ctrl/{year}/displaytype/4.phtml")
    r = get(url)
    html = decode(r)
    print(f"\n########## 3. HTML 资产负债表  HTTP {r.status_code}  长度 {len(html)}")
    # 表头：拿到各列的报告期
    heads = re.findall(r"(\d{4}-\d{2}-\d{2})", html)
    print(f"  页面出现的日期（前 8 个）：{heads[:8]}")
    # 行结构：…/type/<SYMBOL>&cate=zcfz1'>名称</a> 值1 值2 …
    rows = re.findall(
        r"type/([A-Z0-9]+)&cate=zcfz\d?'>([^<]{2,40})</a>([\d,\.\-\s]*)",
        html)
    print(f"  解析到 {len(rows)} 行")
    for sym, name, vals in rows:
        if any(k in name for k in ("归属于母公司", "少数股东权益", "所有者权益合计", "股东权益合计")):
            nums = [v.strip() for v in vals.split() if v.strip()]
            print(f"    [{sym}] {name.strip()} → {nums}")
    # 说明单位：新浪财报页金额单位是「万元」
    m = re.search(r"(单位[:：]\s*[^<>\n]{0,20})", html)
    print(f"  页面单位提示：{m.group(1).strip() if m else '（未找到）'}")


# ---------- 4. 财务指标页（找毛利率） ----------
def probe_guideline(year: int = 2024) -> None:
    url = (f"https://money.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine"
           f"/stockid/{CODE}/ctrl/{year}/displaytype/4.phtml")
    r = get(url)
    html = decode(r)
    print(f"\n########## 4. 财务指标页  HTTP {r.status_code}  长度 {len(html)}")
    if len(html) < 500:
        print(f"  内容过短，前 200 字：{html[:200]!r}")
        return
    rows = re.findall(r"type/([A-Z0-9]+)&cate=[]?\w*'>([^<]{2,40})</a>([\d,\.\-\s]*)", html)
    if not rows:
        rows = re.findall(r">([^<>]{2,30}(?:率|收益|每股)[^<>]{0,10})<[^>]*>([\d,\.\-%\s]*)", html)
    print(f"  解析到 {len(rows)} 行")
    for row in rows[:40]:
        if len(row) == 3:
            sym, name, vals = row
            if re.search(r"率|收益|每股|净资产", name):
                print(f"    [{sym}] {name.strip()} → {vals.strip()[:80]}")


def main() -> int:
    print("=" * 78)
    print(f"新浪补充源字段确认：{CODE}（{PAPER}）")
    print("=" * 78)
    dump_lrb_items()
    retry_zcfz()
    parse_html_balance()
    probe_guideline()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
