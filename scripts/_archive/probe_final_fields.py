"""探针 5：定案 —— 「归母净资产 / 营业成本 / 毛利率」各自的可用源与确切字段。

已知：
  - 东财 F10 资产负债表/现金流量表对保险股整表为空 → 归母净资产缺
  - 新浪 `source=fzb` 的资产负债表 JSON 可用（不是 zcfz）
  - 新浪利润表里平安是「营业收入 / 营业支出」，没有「营业成本」行

待定：
  1. 新浪 fzb JSON 里归母权益的项目名与数值（并与年报/东财权益合计交叉核对）
  2. 平安年报利润表的**真实科目名**（PDF 原文，权威）—— 到底有没有「营业成本」
  3. 同花顺接口对平安给出的「营业成本 / 毛利率」是什么值（券商 App 常用口径）

用法：python scripts/probe_final_fields.py [code]
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
SINA = ("https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService"
        "/getFinanceReport2022")


# ---------- 1. 新浪 fzb：全部项目名 + 2024年报数值 ----------
def sina_report(source: str, want_period: str = "20241231") -> dict:
    p = {"paperCode": PAPER, "source": source, "type": "0", "page": "1", "num": "12"}
    r = requests.get(SINA, params=p, headers=HDR, timeout=config.FETCH_TIMEOUT)
    data = (r.json().get("result") or {}).get("data") or {}
    rl = data.get("report_list") or {}
    dates = {str(d.get("date_value")): d.get("date_description")
             for d in (data.get("report_date") or [])}
    print(f"\n--- 新浪 source={source}：期数 {len(rl)}；期次说明样本 "
          f"{list(dates.items())[:4]}")
    key = want_period if want_period in rl else (sorted(rl, reverse=True)[0] if rl else None)
    if not key:
        print("  无数据")
        return {}
    print(f"  取期 {key}（{dates.get(key)}）")
    items = ((rl.get(key) or {}).get("data")) or []
    out = {}
    for it in items:
        t = str(it.get("item_title", "")).strip()
        out[t] = it.get("item_value")
    return out


def step1_balance() -> dict:
    print("\n########## 1. 新浪资产负债表（fzb）：权益相关项目 ##########")
    items = sina_report("fzb")
    for t, v in items.items():
        if re.search(r"权益|股本|库存股|未分配利润|一般风险", t):
            print(f"    {t} = {v}")
    # 会计恒等式自检（单位：元）
    try:
        parent = float(items.get("归属于母公司的股东权益合计")
                       or items.get("归属于母公司股东权益合计") or 0)
        minority = float(items.get("少数股东权益") or 0)
        total = float(items.get("所有者权益合计") or items.get("股东权益合计") or 0)
        if total:
            print(f"  ✓ 恒等式：归母 {parent/1e8:,.2f}亿 + 少数股东 {minority/1e8:,.2f}亿 "
                  f"= {total/1e8:,.2f}亿；与合计差 {(parent+minority-total)/1e8:,.6f}亿")
    except Exception as e:
        print(f"  恒等式自检跳过：{e}")
    return items


# ---------- 2. 新浪利润表：成本类项目 ----------
def step2_income() -> dict:
    print("\n########## 2. 新浪利润表（lrb）：收入与成本类项目 ##########")
    items = sina_report("lrb")
    for t, v in items.items():
        if re.search(r"营业|成本|支出|收入总额", t):
            print(f"    {t} = {v}")
    return items


# ---------- 3. 平安年报 PDF 原文里的利润表科目 ----------
def step3_pdf() -> None:
    print("\n########## 3. 平安 2024 年报 PDF：合并利润表科目名（权威）##########")
    p = Path("data/parsed/601318/2024.json")
    if not p.exists():
        print("  解析产物不存在，跳过")
        return
    pages = json.loads(p.read_text(encoding="utf-8"))["pages"]
    for pg in pages:
        if "营业利润" not in pg["text"]:
            continue
        if pg.get("section") != "财务报告":
            continue
        lines = [ln.strip() for ln in pg["text"].split("\n") if ln.strip()]
        picked = [ln for ln in lines if re.search(r"营业|支出|成本|利润总额|净利润", ln)]
        if len(picked) < 3:
            continue
        print(f"  P{pg['page_no']}（{len(picked)} 个候选行）")
        for ln in picked[:26]:
            print(f"    {ln[:110]}")
        return
    print("  未定位到利润表页")


# ---------- 4. 同花顺（券商 App 常用口径） ----------
def step4_ths() -> None:
    print("\n########## 4. 同花顺财务接口 ##########")
    cands = [
        f"https://basic.10jqka.com.cn/api/stock/finance/{CODE}_main.json",
        f"https://basic.10jqka.com.cn/api/stock/finance/{CODE}_debt.json",
        f"https://basic.10jqka.com.cn/api/stock/finance/{CODE}_benefit.json",
        f"https://basic.10jqka.com.cn/api/stock/finance/{CODE}_cash.json",
    ]
    for url in cands:
        try:
            r = requests.get(url, headers={**HDR, "Referer": "https://basic.10jqka.com.cn/"},
                             timeout=config.FETCH_TIMEOUT)
            ctype = r.headers.get("Content-Type", "")
            print(f"\n--- {url.rsplit('/', 1)[-1]}  HTTP {r.status_code}  {ctype}  长度 {len(r.content)}")
            txt = r.content.decode("utf-8", errors="replace")
            if r.status_code == 200 and ctype.startswith("application/json"):
                j = json.loads(txt)
                print(f"  顶层键：{sorted(j)[:12]}")
                flat = json.dumps(j, ensure_ascii=False)
                for kw in ("毛利率", "营业成本", "净资产", "股东权益"):
                    m = re.search(re.escape(kw) + r"[^,{}\[\]]{0,40}", flat)
                    print(f"    [{kw}] {m.group(0)[:70] if m else '未出现'}")
            else:
                print(f"  前 160 字：{txt[:160]!r}")
        except Exception as e:
            print(f"\n--- {url.rsplit('/', 1)[-1]}\n  EXC {type(e).__name__}: {e}")


def main() -> int:
    print("=" * 78)
    print(f"定案探针：{CODE}（{PAPER}）")
    print("=" * 78)
    step1_balance()
    step2_income()
    step3_pdf()
    step4_ths()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
