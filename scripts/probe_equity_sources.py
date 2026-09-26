"""探针 2：为「归母净资产」找可用源，并诊断老版 F10 Ajax 接口。

候选顺序（先零依赖，再考虑第三方库）：
  A. 主要指标接口的**全部列名**（含值为 null 的）—— 看有没有我们漏登记的权益字段
  B. 老版 F10 Ajax —— 打印 HTTP 状态 / content-type / 前 300 字，判断是 403、404 还是接口改名
  C. 新浪财经三大报表（HTML 表格，正则可解；零依赖）
  D. 同花顺 / 雪球 JSON 接口（雪球需 cookie，试一下）
  E. 东财 H 股（中国平安 02318.HK）资产负债表 —— 仍是东财，无需新依赖

用法：python scripts/probe_equity_sources.py [code]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from src import config, net  # noqa: E402

CODE = sys.argv[1] if len(sys.argv) > 1 else "601318"
SECUCODE = f"{CODE}.SH" if CODE.startswith(("6", "9")) else f"{CODE}.SZ"
HCODE = {"601318": "02318"}.get(CODE)      # A→H 只在有对应 H 股时用
UA = config.FETCH_UA
WANT = ["归属于母公司股东权益", "归属母公司股东权益", "母公司股东权益",
        "少数股东权益", "股东权益合计", "所有者权益合计"]


def dump_cols(label: str, cols: list[str], pattern: str) -> None:
    print(f"\n--- {label}：列名含 /{pattern}/ 的全部列（不论是否有值）")
    hits = [c for c in cols if re.search(pattern, c, re.I)]
    print(f"  共 {len(cols)} 列；命中 {len(hits)} 个：{hits if hits else '（无）'}")


def try_http(label: str, url: str, headers: dict | None = None) -> str | None:
    """发一次请求并打印诊断信息；返回文本（失败返回 None）。"""
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers or {})
    try:
        r = requests.get(url, headers=h, timeout=config.FETCH_TIMEOUT)
    except Exception as e:
        print(f"\n--- {label}\n  EXC {type(e).__name__}: {e}")
        return None
    ctype = r.headers.get("Content-Type", "")
    print(f"\n--- {label}\n  HTTP {r.status_code}  {ctype}  长度 {len(r.content)}")
    text = None
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            text = r.content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        print("  解码失败")
        return None
    print(f"  前 200 字：{text[:200]!r}")
    return text


def scan_text_for_equity(label: str, text: str) -> None:
    """在 HTML/文本里找权益相关行，打印邻近片段。"""
    hits = 0
    for w in WANT:
        for m in re.finditer(re.escape(w), text):
            s = max(0, m.start() - 60)
            e = min(len(text), m.end() + 200)
            frag = re.sub(r"<[^>]+>", " ", text[s:e])
            frag = re.sub(r"\s+", " ", frag).strip()
            print(f"  [{w}] …{frag[:230]}…")
            hits += 1
            break                      # 每个关键词只看第一处，避免刷屏
    if not hits:
        print("  未命中任何权益关键词")


def main() -> int:
    print("=" * 78)
    print(f"目标 {CODE}（{SECUCODE}）")
    print("=" * 78)

    # ---------- A. 主要指标全部列名 ----------
    print("\n########## A. 主要指标接口的全部列名 ##########")
    for rn in ("RPT_F10_FINANCE_MAINFINADATA",):
        params = {"reportName": rn, "columns": "ALL", "pageSize": 3, "pageNumber": 1,
                  "filter": f'(SECUCODE="{SECUCODE}")',
                  "sortColumns": "REPORT_DATE", "sortTypes": -1}
        d = net.get_json(config.EASTMONEY_SECURITIES_API, params=params)
        rows = ((d or {}).get("result") or {}).get("data") or []
        if rows:
            dump_cols(rn, sorted(rows[0].keys()), r"EQUITY|权益|BPS|PARENT|SHAREHOLDER")

    # ---------- B. 老版 F10 Ajax 诊断 ----------
    print("\n########## B. 老版 F10 Ajax 诊断 ##########")
    base = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
    for label, path in {
        "资产负债表": f"ZCFZBAjaxNew?type=0&code=SH{CODE}",
        "利润表": f"LRBAjaxNew?type=0&code=SH{CODE}",
    }.items():
        try_http(f"老版 {label}",
                 f"{base}/{path}",
                 {"Referer": "https://emweb.securities.eastmoney.com/PC_HSF10/FinancialAnalysis/index"})

    # ---------- C. 新浪财经三大报表 ----------
    print("\n########## C. 新浪财经（HTML 表格）##########")
    print("\n【注意】新浪财报页的控件参数是「报告期」，未指定时返回全部期次表格")
    urls = {
        "资产负债表": f"https://money.finance.sina.com.cn/corp/go.php/vFD_BalanceSheet/stockid/{CODE}/ctrl/2024/displaytype/4.phtml",
        "利润表": f"https://money.finance.sina.com.cn/corp/go.php/vFD_ProfitStatement/stockid/{CODE}/ctrl/2024/displaytype/4.phtml",
    }
    for label, url in urls.items():
        text = try_http(f"新浪 {label}", url, {"Referer": "https://finance.sina.com.cn/"})
        if text:
            scan_text_for_equity(f"新浪 {label}", text)

    # ---------- D. 雪球（需 cookie，试一下） ----------
    print("\n########## D. 雪球 JSON ##########")
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    try:
        s.get("https://xueqiu.com/", timeout=15)      # 取 cookie
        r = s.get(f"https://stock.xueqiu.com/v5/stock/finance/cn/balance.json"
                  f"?symbol=SH{CODE}&type=Q4&is_detail=true&count=3",
                  timeout=20)
        print(f"  HTTP {r.status_code}  {r.headers.get('Content-Type','')}")
        j = r.json()
        items = (((j or {}).get("data") or {}).get("list") or [])
        print(f"  期数 {len(items)}")
        if items:
            keys = [k for k in items[0] if re.search(r"equity|权益|parent|minority", k, re.I)]
            print(f"  权益相关键：{keys}")
            for k in keys:
                print(f"    {k} = {items[0][k]}")
    except Exception as e:
        print(f"  EXC {type(e).__name__}: {e}")

    # ---------- E. 东财 H 股资产负债表 ----------
    if HCODE:
        print("\n########## E. 东财 H 股（02318.HK）资产负债表 ##########")
        for rn in ("RPT_HKF10_FN_BALANCE", "RPT_HKF10_FN_BALANCE_HK"):
            params = {"reportName": rn, "columns": "ALL", "pageSize": 3, "pageNumber": 1,
                      "filter": f'(SECUCODE="{HCODE}.HK")',
                      "sortColumns": "REPORT_DATE", "sortTypes": -1}
            try:
                d = net.get_json(config.EASTMONEY_SECURITIES_API, params=params)
            except Exception as e:
                print(f"\n--- {rn}\n  EXC {type(e).__name__}: {e}")
                continue
            ok = (d or {}).get("success")
            print(f"\n--- {rn}  success={ok} message={(d or {}).get('message')!r}")
            rows = ((d or {}).get("result") or {}).get("data") or []
            if rows:
                dump_cols(rn, sorted(rows[0].keys()), r"EQUITY|权益|PARENT|MINORITY")
                print(f"  样本期 {rows[0].get('REPORT_DATE')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
