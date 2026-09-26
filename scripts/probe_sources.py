"""数据源探活 —— 把「实测过的可用性」固化成可复跑的自检。

为什么要有这个脚本：
取数环节的所有坑都是「参数对不对」的问题，而它们的失败表现**极其一致**：
巨潮参数写错返回 0 条、新浪不带 Referer 返回 403 —— 都长得像"网络不通"。
这个脚本把每个源的正确姿势写死，出问题时一眼能定位是"源挂了"还是"参数错了"。

用法：
    python scripts/probe_sources.py            # 全量（含真实 PDF 可达性）
    python scripts/probe_sources.py --quick    # 跳过网络较重的 PDF 探测
退出码：关键源有失败则 1（可选源失败不影响退出码）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from src import config, net  # noqa: E402
from src.ingest import fetch_cninfo  # noqa: E402

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"


def probe_cninfo_search() -> tuple[str, str]:
    """巨潮 orgId 查询 —— 取数的第一步，查不到 orgId 后面全废。"""
    rows = net.post_form_json(
        fetch_cninfo.CNINFO_SEARCH_URL,
        {"keyWord": "600519", "maxNum": 10},
        referer=fetch_cninfo.CNINFO_REFERER)
    if not isinstance(rows, list) or not rows:
        return FAIL, "返回空列表（接口改版或被限流）"
    a = [r for r in rows if r.get("category") == "A股"]
    if not a:
        return FAIL, f"有返回但无 A 股条目：{[r.get('category') for r in rows][:3]}"
    r0 = a[0]
    return OK, f"{r0.get('code')} {r0.get('zwjc')} orgId={r0.get('orgId')}（共 {len(rows)} 条）"


def probe_cninfo_announcement() -> tuple[str, str]:
    """年报公告查询 —— 这里最容易踩「stock 没传两段式 => 静默返回 0 条」。"""
    org_id = fetch_cninfo.lookup_org_id("600519", code="600519")
    if not org_id:
        return FAIL, "拿不到 orgId，无法继续"
    anns = fetch_cninfo.query_annual_reports("600519", org_id, "2022-01-01", "2026-12-31")
    if not anns:
        return FAIL, "0 条年报（检查 stock 是否为 `代码,orgId` 两段式、column 是否匹配市场）"
    newest = anns[-1]
    return OK, (f"查到 {len(anns)} 份年报，最新 {newest['year']}年 "
                f"{newest['size_kb']}KB → {newest['url']}")


def probe_cninfo_pdf() -> tuple[str, str]:
    """PDF 是否真的可下（只取前 16KB，别下整份）。"""
    org_id = fetch_cninfo.lookup_org_id("600519", code="600519")
    anns = fetch_cninfo.query_annual_reports("600519", org_id or "", "2022-01-01", "2026-12-31")
    if not anns:
        return SKIP, "没有可用的公告 URL"
    url = anns[-1]["url"]
    r = requests.get(url, headers={"User-Agent": config.FETCH_UA},
                     timeout=config.FETCH_TIMEOUT, stream=True)
    try:
        if r.status_code != 200:
            return FAIL, f"HTTP {r.status_code}"
        head = next(r.iter_content(16384), b"")
    finally:
        r.close()
    if not head.startswith(b"%PDF"):
        return FAIL, f"返回的不是 PDF（前 8 字节：{head[:8]!r}）"
    return OK, f"{url.rsplit('/', 1)[-1]} 头部 {len(head)} 字节是合法 PDF"


def probe_eastmoney_datacenter() -> tuple[str, str]:
    """东财数据中心 —— Step 2 的三大报表数据来源。"""
    data = net.get_json(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={
            "reportName": "RPT_LICO_FN_CPD",
            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,"
                       "TOTAL_OPERATE_INCOME,PARENT_NETPROFIT",
            "pageSize": 2, "sortColumns": "REPORTDATE", "sortTypes": -1,
        })
    rows = ((data or {}).get("result") or {}).get("data") or []
    if not rows:
        return FAIL, f"无数据：{str(data)[:160]}"
    r0 = rows[0]
    return OK, (f"{r0.get('SECURITY_NAME_ABBR')} {r0.get('REPORTDATE', '')[:10]} "
                f"营收={r0.get('TOTAL_OPERATE_INCOME')} 归母净利={r0.get('PARENT_NETPROFIT')}")


def probe_eastmoney_quote() -> tuple[str, str]:
    """行情列表。注意：东财在参数不匹配时会把 `diff` 返回成字符串（如 "-"），
    不是列表 —— 直接迭代会得到单个字符并抛 AttributeError，必须判类型。"""
    data = net.get_json("https://push2.eastmoney.com/api/qt/clist/get",
                        params={"pn": 1, "pz": 3, "fs": "m:0+t:6", "fields": "f12,f14"})
    diff = ((data or {}).get("data") or {}).get("diff")
    if not isinstance(diff, list) or not diff:
        return FAIL, f"diff 不是非空列表（实际 {type(diff).__name__}: {str(diff)[:80]}）"
    return OK, "、".join(f"{d.get('f14')}({d.get('f12')})" for d in diff)


def probe_sina_with_referer() -> tuple[str, str]:
    """新浪必须带 Referer —— 下面 probe_sina_without_referer 专门验证这一点。"""
    r = requests.get("https://hq.sinajs.cn/list=sh600519",
                     headers={"User-Agent": config.FETCH_UA,
                              "Referer": config.SINA_REFERER},
                     timeout=config.FETCH_TIMEOUT)
    if r.status_code != 200:
        return FAIL, f"HTTP {r.status_code}"
    if "hq_str_sh600519" not in r.text:
        return FAIL, f"返回体异常：{r.text[:80]!r}"
    return OK, r.text.split('"')[1].split(",")[0] + "（带 Referer 正常返回）"


def probe_sina_without_referer() -> tuple[str, str]:
    """反向验证：不带 Referer 应该 403。这条过了说明我们确实搞懂了原因，而不是碰巧能用。"""
    r = requests.get("https://hq.sinajs.cn/list=sh600519",
                     headers={"User-Agent": config.FETCH_UA},
                     timeout=config.FETCH_TIMEOUT)
    if r.status_code == 403:
        return OK, "不带 Referer 果然 403（与我们记录的一致）"
    return SKIP, f"实际返回 HTTP {r.status_code}（反爬策略可能已变，正文里同步一下）"


PROBES: list[tuple[str, str, bool, callable]] = [
    # (分组, 名称, 是否关键, 函数)
    ("巨潮资讯", "orgId 查询（topSearch）", True, probe_cninfo_search),
    ("巨潮资讯", "年报公告查询（hisAnnouncement）", True, probe_cninfo_announcement),
    ("东财", "数据中心（三大报表来源）", True, probe_eastmoney_datacenter),
    ("东财", "行情列表", False, probe_eastmoney_quote),
    ("新浪", "行情（带 Referer）", False, probe_sina_with_referer),
    ("新浪", "反向验证：不带 Referer 应 403", False, probe_sina_without_referer),
]
HEAVY = [("巨潮资讯", "年报 PDF 可达性（取前 16KB）", False, probe_cninfo_pdf)]


def main() -> int:
    quick = "--quick" in sys.argv
    items = PROBES + ([] if quick else HEAVY)

    print(f"数据源探活（UA={config.FETCH_UA[:40]}... 间隔={config.FETCH_INTERVAL}s"
          f"{'，quick 模式' if quick else ''}）\n")
    failures: list[str] = []
    last_group = None
    for group, name, critical, fn in items:
        if group != last_group:
            print(f"[{group}]")
            last_group = group
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        flag = "✗" if status == FAIL else ("·" if status == SKIP else "✓")
        print(f"  {flag} {name:<34} {detail}")
        if status == FAIL and critical:
            failures.append(f"{group}/{name}")

    print()
    if failures:
        print(f"❌ 关键源失败 {len(failures)} 项：{failures}")
        print("   提示：巨潮/新浪的失败多半是参数问题，不是网络问题 —— "
              "先核对 stock 是否两段式、column 是否匹配市场、Referer 是否带上。")
        return 1
    print("✅ 关键数据源全部可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
