"""Step 2 验收核对 —— 库层数值 + 工具层行为，一条命令跑完。

用法：
    python scripts/verify_step2_db.py

分五段，每段都打印"可手算复核"的证据，而不是只喊 OK：
  §1 库内计数与按指标覆盖
  §2 会计恒等式（总资产 − 总负债 = 所有者权益合计）
  §3 工具层数值验收（对应实施方案 Step 2 的三条验收标准）
  §4 保险股口径边界（缺什么、是否如实报缺，而不是编一个数）
  §5 注册表错误兜底（LLM 传参不可信）

为什么把断言写成"打印 + 判定"而不是 pytest：
pytest 跑的是合成数据的**规则**正确性；本脚本跑的是真实取数的**数值**正确性。
后者依赖网络数据，不适合放进每次 CI，但每次改口径表后都该手工跑一遍。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402
from src.tools import dispatch  # noqa: E402
from src.tools.indicators import compare_companies, get_financial_indicator  # noqa: E402
from src.tools.ratios import calc_financial_ratio  # noqa: E402

PERIOD = "2024-12-31"
KEYS = ["总资产", "总负债", "所有者权益合计", "归母净资产", "毛利率", "营业成本",
        "经营活动现金流净额", "投资活动现金流净额", "筹资活动现金流净额",
        "营业总收入", "营业收入", "归母净利润", "ROE", "资产负债率"]


def yi(v):
    return f"{v / 1e8:,.2f}亿" if v is not None else "—"


def sec(n: int, title: str) -> None:
    print(f"\n{'=' * 78}\n§{n} {title}\n{'=' * 78}")


def main() -> int:
    sec(1, "库内计数与指标覆盖")
    print(f"后端 {config.DB_BACKEND}  库 {config.DB_PATH}")
    with db.get_conn() as conn:
        for t in db.ALL_TABLES:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
            print(f"  {t:<22} {(row['n'] if isinstance(row, dict) else row[0]):>6} 行")
        per = conn.execute(
            "SELECT indicator, COUNT(*) AS n FROM financial_indicators "
            "GROUP BY indicator ORDER BY n, indicator").fetchall()
    print("  " + "  ".join(
        f"{(r['indicator'] if isinstance(r, dict) else r[0])}="
        f"{r['n'] if isinstance(r, dict) else r[1]}" for r in per))

    sec(2, f"会计恒等式（{PERIOD}）：总资产 − 总负债 = 所有者权益合计")
    marks = ", ".join("?" * len(KEYS))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT code, indicator, value, unit, source_table, source_field "
            f"FROM financial_indicators WHERE period=? AND indicator IN ({marks})",
            [PERIOD, *KEYS]).fetchall()
    rows = [dict(r) if isinstance(r, dict) else {k: r[k] for k in r.keys()} for r in rows]
    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(r["code"], {})[r["indicator"]] = r
    ok_all = True
    for code, m in sorted(by.items()):
        a, l, e = (m.get(k, {}).get("value") for k in
                   ("总资产", "总负债", "所有者权益合计"))
        if None in (a, l, e):
            print(f"  — {code}: 数据不全，跳过")
            continue
        rel = abs(a - l - e) / a * 100
        good = rel < 0.5
        ok_all &= good
        print(f"  {'✓' if good else '✗'} {code}: {yi(a)} − {yi(l)} = {yi(a - l)} "
              f"vs 权益 {yi(e)}  差 {rel:.4f}%")
        # 顺带把「实际命中的源」打出来：跨源拼出来的恒等式能成立，才说明没串口径
        print(f"      源：总资产={m['总资产']['source_table']}.{m['总资产']['source_field']}，"
              f"权益={m['所有者权益合计']['source_table']}."
              f"{m['所有者权益合计']['source_field']}")

    sec(3, "工具层验收（对应实施方案 Step 2 的三条标准）")
    # 3.1 get_financial_indicator 返回正确序列与单位
    got = get_financial_indicator("贵州茅台", "营业总收入", periods=3)
    assert got["ok"], got
    print(f"  ✓ get_financial_indicator(贵州茅台, 营业总收入) "
          f"→ 单位 {got['indicator']['unit']}（展示 {got['indicator']['display_unit']}）")
    for s in got["series"]:
        print(f"      {s['period']}  {s['display']:<16} <- {s['source']}")

    # 3.2 calc_financial_ratio 的分子分母可核对
    ratio = calc_financial_ratio("贵州茅台", "毛利率", period=PERIOD)
    assert ratio["ok"], ratio
    print(f"\n  ✓ calc_financial_ratio(贵州茅台, 毛利率) = {ratio['display']}")
    print(f"      公式：{ratio['ratio']['formula']}")
    print(f"      分子：{ratio['numerator']['display']}")
    for t in ratio["numerator"]["terms"]:
        print(f"            {t['sign']} {t['indicator']:<8} {t['display']:<16} <- {t['source']}")
    print(f"      分母：{ratio['denominator']['display']}")
    for t in ratio["denominator"]["terms"]:
        print(f"            {t['sign']} {t['indicator']:<8} {t['display']:<16} <- {t['source']}")
    if ratio["official"]:
        print(f"      官方口径 {ratio['official']['value']:.4f} "
              f"({ratio['official']['source']})，派生差 "
              f"{ratio['cross_check']['delta_display']} → {ratio['cross_check']['verdict']}")

    # 3.3 compare_companies 排名
    cmp_ = compare_companies(["贵州茅台", "五粮液", "宁德时代", "比亚迪", "中国平安"], "ROE")
    assert cmp_["ok"], cmp_
    print(f"\n  ✓ compare_companies(5 家, ROE) 期次一致={cmp_['periods_consistent']}")
    for r in cmp_["rows"]:
        print(f"      #{r['rank']} {r['name']:<8} {r['display']:<10} {r['period']}  "
              f"[{r['industry']}] <- {r['source']}")
    if cmp_["note"]:
        print(f"      注：{cmp_['note']}")

    sec(4, "保险股口径边界（该缺的就缺，不编数）")
    p = calc_financial_ratio("中国平安", "毛利率")
    print(f"  毛利率：ok={p['ok']} error={p.get('error')} "
          f"needed={p.get('needed_indicators')}")
    print(f"      {p.get('message')}")
    r = calc_financial_ratio("中国平安", "ROE")
    print(f"  ROE   ：ok={r['ok']} error={r.get('error')} "
          f"missing={r.get('missing_indicators')}")
    d = calc_financial_ratio("中国平安", "资产负债率", period=PERIOD)
    print(f"  资产负债率：ok={d['ok']} {d.get('display')}  "
          f"官方={d['official']['display'] if d.get('official') else '—'}  "
          f"→ {d['cross_check']['verdict'] if d.get('cross_check') else '—'}")
    g = get_financial_indicator("中国平安", "毛利率")
    print(f"  get_financial_indicator(中国平安, 毛利率)：ok={g['ok']} count={g['count']}"
          f"（不报错、只报「无数据 + 说明」）")
    print(f"      {g.get('note')}")

    sec("4.5", "第二源补缺（东财拿不到的，去别处拿 —— 不是宣布「数据源没有」）")
    # 归母净资产：东财全系都不提供保险股这一项，只有新浪 fzb 有。
    net_ = get_financial_indicator("中国平安", "归母净资产", periods=3)
    print(f"  归母净资产：ok={net_['ok']} count={net_['count']}（源应为新浪，不是东财）")
    for s in net_["series"]:
        print(f"      {s['period']}  {s['display']:<16} <- {s['source']}")
    assert net_["count"] > 0, "保险股归母净资产应已由新浪源补齐"
    assert all(s["source"].startswith("sina_balance") for s in net_["series"]), \
        "保险股归母净资产必须来自新浪；若变成东财源，说明口径表被改坏了"

    # 营业成本对保险不存在 → 必须给出替代科目，而不是只说"没有"
    cost = get_financial_indicator("中国平安", "营业成本")
    cp = cost.get("counterpart") or {}
    print(f"\n  营业成本：count={cost['count']}（保险业无此科目）")
    print(f"      counterpart → {cp.get('indicator', {}).get('name')} "
          f"{cp.get('indicator', {}).get('latest', {}).get('display')} "
          f"<- {cp.get('indicator', {}).get('latest', {}).get('source')}")
    assert cp.get("indicator"), "营业成本对保险应给出「营业支出」替代项"

    # 毛利率对保险不存在 → 必须给出同义替代口径及数值
    alt = (calc_financial_ratio("中国平安", "毛利率").get("alternatives") or [{}])[0]
    print(f"\n  毛利率 替代口径 → {alt.get('ratio')} = {alt.get('display')}  "
          f"（{alt.get('formula')}）")
    for t in (alt.get("numerator") or {}).get("terms", []):
        print(f"      {t['sign']} {t['indicator']:<8} {t['display']:<16} <- {t['source']}")
    assert alt.get("ok"), "应能算出保险口径毛利率"

    sec(5, "注册表错误兜底（LLM 传参不可信）")
    for args, label in [
        (({"company": "贵州茅台"}, "get_financial_indicator"), "漏必填参数"),
        (({"company": "贵州茅台", "indicator": "营业总收入", "unit": "元"},
          "get_financial_indicator"), "多传参数"),
        (({}, "不存在的工具"), "编造工具名"),
        (({"company": "贵州茅台", "indicator": "瞎写的指标"},
          "get_financial_indicator"), "指标名不认识"),
    ]:
        out = dispatch(args[1], args[0])
        print(f"  {label:<12} → ok={out['ok']} error={out.get('error')}")
        print(f"      {str(out.get('message', ''))[:100]}")

    print(f"\n{'=' * 78}")
    print(f"结论：会计恒等式{'全部通过' if ok_all else '存在不一致'}；工具层各类返回均符合预期。")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
