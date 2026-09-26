"""探针 6：同花顺财务接口里的「营业成本 / 毛利率 / 归母净资产」命名与口径。

券商 App 的财务页多用同花顺口径。它的接口返回体是 `{"flashData": "<转义过的 JSON 字符串>"}`
（不是嵌套对象），所以必须先解一层字符串再解析。这里确认：
  1. 它把保险的成本行叫什么（营业成本？营业支出？）
  2. 它是否直接给出「毛利率」
  3. 归母净资产叫什么

只作**交叉验证**用，不一定要接入（多一个爬虫源就多一份维护成本）。
用法：python scripts/probe_ths_fields.py [code]
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
BASE = "https://basic.10jqka.com.cn/api/stock/finance"
HDR = {"User-Agent": config.FETCH_UA, "Accept": "*/*",
       "Referer": "https://basic.10jqka.com.cn/"}
KEYS = ("营业成本", "营业支出", "营业总成本", "毛利率", "净资产", "股东权益", "营业收入")


def load(kind: str) -> object | None:
    url = f"{BASE}/{CODE}_{kind}.json"
    try:
        r = requests.get(url, headers=HDR, timeout=config.FETCH_TIMEOUT)
    except Exception as e:
        print(f"\n--- {kind}: EXC {type(e).__name__}: {e}")
        return None
    print(f"\n--- {kind}: HTTP {r.status_code}  长度 {len(r.content)}")
    txt = r.content.decode("utf-8", errors="replace")
    try:
        outer = json.loads(txt)
    except Exception as e:
        print(f"  外层 JSON 解析失败：{e}")
        return None
    print(f"  外层键：{sorted(outer)}")
    inner = outer.get("flashData") or outer.get("data")
    if isinstance(inner, str):
        # flashData 是被转义过的 JSON 字符串：先按 JSON 字符串解，再按 JSON 解
        try:
            inner = json.loads(inner)
        except Exception:
            while isinstance(inner, str):
                inner = json.loads(inner)
    return inner


def scan(label: str, obj: object) -> None:
    flat = json.dumps(obj, ensure_ascii=False)
    print(f"  [{label}] 序列化长度 {len(flat)}")
    for kw in KEYS:
        idxs = [m.start() for m in re.finditer(re.escape(kw), flat)]
        if not idxs:
            continue
        print(f"    ★ {kw}：出现 {len(idxs)} 次，样本：")
        for i in idxs[:2]:
            print(f"        …{flat[max(0, i - 40):i + 90]}…")


def main() -> int:
    print("=" * 78)
    print(f"同花顺字段确认：{CODE}")
    print("=" * 78)
    for kind in ("main", "benefit", "debt"):
        obj = load(kind)
        if obj is None:
            continue
        scan(kind, obj)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
