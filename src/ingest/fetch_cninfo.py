"""巨潮资讯：查年报公告 → 下载 PDF 原件。

这是整个项目「溯源」能力的地基——没有原始 PDF，就没有页码可引。

实测踩坑（写死在这里，别再调）：
1. `hisAnnouncement/query` 的 `stock` 参数必须是 **`代码,orgId`** 两段式，
   只传 `600519` 会返回 **0 条**（静默空数组，很容易误判成被墙）。
2. `column` 必须与市场匹配（沪市 `sse` / 深市 `szse`），不匹配同样返回 0 条。
3. **orgId 不能靠代码推**：600519→`gssh0600519` 看似有规律，
   但 300750→`GD165627`、002594→`gshk0001211`、601318→`9900002221` 毫无规律。
   → 一律走 `topSearch/query` 查，查到后写回 watchlist.yaml 缓存，避免每次都查。
4. PDF 地址 = `http://static.cninfo.com.cn/` + 返回体里的 `adjunctUrl`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src import config, net

CNINFO_SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "http://static.cninfo.com.cn/"
CNINFO_REFERER = "http://www.cninfo.com.cn/"

# 年报分类（沪深通用）
CATEGORY_ANNUAL = "category_ndbg_szsh"

# 标题里出现这些词就不是我们想要的那份年报
_TITLE_BLOCK = ("摘要", "英文", "English", "取消", "补充", "说明", "提示性公告")
_YEAR_RE = re.compile(r"(20\d{2})\s*年\s*年度报告")
_ANNUAL_RE = re.compile(r"年度报告")


def market_of(code: str) -> str:
    """按代码判断巨潮的 column。错了就是 0 条，所以这里宁可显式报错也不猜。"""
    if code.startswith("6"):
        return "sse"        # 沪市（含 688 科创板）
    if code.startswith(("0", "3")):
        return "szse"       # 深市（含 300 创业板）
    if code.startswith(("4", "8")):
        return "bj"         # 北交所
    raise ValueError(f"无法判断市场：{code}（请在 watchlist 里显式给 market）")


def lookup_org_id(keyword: str, code: str | None = None) -> str | None:
    """按代码或名称查 orgId。优先精确匹配 A 股代码，其次匹配简称。"""
    rows = net.post_form_json(
        CNINFO_SEARCH_URL, {"keyWord": keyword, "maxNum": 10}, referer=CNINFO_REFERER)
    if not isinstance(rows, list):
        return None
    a_shares = [r for r in rows if r.get("category") == "A股"]
    if code:
        for r in a_shares:
            if str(r.get("code")) == str(code):
                return str(r.get("orgId") or "") or None
    if len(a_shares) == 1:
        return str(a_shares[0].get("orgId") or "") or None
    return None


def query_annual_reports(code: str, org_id: str, date_from: str, date_to: str,
                         market: str | None = None) -> list[dict]:
    """查某公司年报公告列表（已过滤摘要/英文版，同一年保留最新一条）。

    返回每条含：year / title / announcement_time / announcement_id / url
    """
    payload = {
        "pageNum": 1,
        "pageSize": 30,
        "column": market or market_of(code),
        "tabName": "fulltext",
        "stock": f"{code},{org_id}",      # ← 必须是两段式，见模块 docstring
        "category": CATEGORY_ANNUAL,
        "seDate": f"{date_from}~{date_to}",
        "isHLtitle": "true",
    }
    data = net.post_form_json(CNINFO_QUERY_URL, payload, referer=CNINFO_REFERER)
    rows = (data or {}).get("announcements") or []

    by_year: dict[int, dict] = {}
    for r in rows:
        title = (r.get("announcementTitle") or "").replace("<em>", "").replace("</em>", "")
        if not _ANNUAL_RE.search(title) or any(b in title for b in _TITLE_BLOCK):
            continue
        m = _YEAR_RE.search(title)
        if not m:
            continue
        year = int(m.group(1))
        ts = int(r.get("announcementTime") or 0)
        item = {
            "year": year,
            "title": title,
            "announcement_time": ts,
            "announcement_id": r.get("announcementId"),
            "url": CNINFO_STATIC + str(r.get("adjunctUrl") or ""),
            "size_kb": r.get("adjunctSize"),
        }
        # 同一年可能有多份（更正版、修订版）：留 ts 最新的
        if year not in by_year or ts > by_year[year]["announcement_time"]:
            by_year[year] = item
    return [by_year[y] for y in sorted(by_year)]


def manifest_path(code: str) -> Path:
    return config.RAW_DIR / code / "manifest.json"


def load_manifest(code: str) -> dict:
    p = manifest_path(code)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_manifest(code: str, manifest: dict) -> None:
    p = manifest_path(code)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def download_reports(code: str, years: list[int], *, org_id: str | None = None,
                     name: str = "", market: str | None = None,
                     date_from: str = "2018-01-01", date_to: str = "2030-12-31",
                     force: bool = False) -> dict:
    """下载指定年份的年报 PDF，返回汇总结果。

    幂等：已存在且字节数与 manifest 记录一致则跳过（`force=True` 强制重下）。
    每家公司维护 `data/raw/{code}/manifest.json`，记录公告 id/标题/时间/URL/字节数
    —— 这份 manifest 就是后续溯源里「原始出处」的凭据。
    """
    if not org_id:
        org_id = lookup_org_id(code, code=code) or ""
    if not org_id:
        return {"code": code, "name": name, "ok": False,
                "error": "查不到 orgId（巨潮搜索无匹配 A 股）", "files": []}

    anns = query_annual_reports(code, org_id, date_from, date_to, market=market)
    if years:
        anns = [a for a in anns if a["year"] in years]

    manifest = load_manifest(code)
    files, skipped, failed = [], 0, []
    for a in anns:
        dest = config.RAW_DIR / code / f"{a['year']}_annual.pdf"
        expected = (manifest.get(str(a["year"])) or {}).get("bytes")
        if dest.exists() and not force:
            if expected and dest.stat().st_size == expected:
                skipped += 1
                files.append(str(dest))
                continue
            if not expected:
                # 文件在但没记录（手工放的）：补一条记录，不重下
                manifest[str(a["year"])] = {**a, "bytes": dest.stat().st_size, "local": str(dest)}
                skipped += 1
                files.append(str(dest))
                continue
        try:
            n = net.download(a["url"], dest, referer=CNINFO_REFERER)
        except Exception as e:
            failed.append({"year": a["year"], "error": f"{type(e).__name__}: {e}"})
            continue
        manifest[str(a["year"])] = {**a, "bytes": n, "local": str(dest)}
        files.append(str(dest))

    manifest["_code"] = code
    manifest["_name"] = name
    manifest["_org_id"] = org_id
    manifest["_market"] = market or market_of(code)
    save_manifest(code, manifest)

    return {"code": code, "name": name, "ok": not failed, "org_id": org_id,
            "market": manifest["_market"], "downloaded": len(files) - skipped,
            "skipped": skipped, "failed": failed, "files": files}


def fetch_watchlist(years: list[int] | None = None, force: bool = False) -> list[dict]:
    """按 config/watchlist.yaml 全量取数。"""
    results = []
    for c in config.load_watchlist():
        code = str(c.get("code", "")).strip()
        if not code:
            continue
        r = download_reports(
            code,
            years if years is not None else list(c.get("years") or []),
            org_id=str(c.get("org_id") or "").strip() or None,
            name=str(c.get("name") or ""),
            market=str(c.get("market") or "").strip() or None,
            force=force,
        )
        results.append(r)
    return results


if __name__ == "__main__":
    # 自检：python -m src.ingest.fetch_cninfo
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    print("market_of 自检:", {c: market_of(c) for c in ("600519", "300750", "002594", "601318")})
    print("orgId 自检:", {c: lookup_org_id(c, code=c) for c in ("600519", "300750", "002594")})
    anns = query_annual_reports("600519", lookup_org_id("600519", "600519") or "", "2022-01-01", "2026-12-31")
    for a in anns:
        print(f"  {a['year']}  {a['title']}  {a['size_kb']}KB  {a['url']}")
