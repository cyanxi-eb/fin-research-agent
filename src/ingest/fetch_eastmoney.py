"""东财 + 新浪财务数据取数 —— 三大报表 + 主要指标 → 清洗入 financial_indicators。

取数原则（Step 2 的核心纪律）：
1. **字段名只从 config.INDICATORS 取**，不在代码里另写一份。
   口径表是唯一事实来源，代码只做「按口径搬数」，换指标/加指标只改配置。
2. **每个落库的值都带实际命中的 source_key + field**，让值能自证口径。
   注意这里落的是**实际命中**的源，不是配置里声明的首选源 —— 保险股会回退到别的源，
   如果落"首选源"就是在说谎。
3. **多源优先级回退**：保险公司（实测中国平安 601318）在 F10 的资产负债表与现金流量表
   接口下**整表返回空**（`success=false`），只有数据中心批量报表拿得到；而数据中心报表
   没有 `TOTAL_PARENT_EQUITY` 这类列，所以按指标逐条声明优先级，代码依次取第一个非空值。
4. **东财拿不到的要去找第二源，而不是宣布"数据源没有"**：
   东财全系都没有保险股的「归属于母公司股东权益」——而券商 App 上明明有。
   已接入**新浪财经**资产负债表（`source=fzb`）补这一项，实测恒等式零误差。
   教训：把"我没取到"当成"数据不存在"是错的，两种结论的证据强度完全不同。
5. 字段名**全部实测确认**（scripts/probe_*.py），不凭记忆写。
   实测对账：茅台 2024 年报 营业总收入 1741.44 亿 / 归母净利润 862.28 亿 /
   加权 ROE 36.02% / 毛利率 91.93% / 资产负债率 19.04%，与年报原文一致，单位为元。

用法：
    python -m src.ingest.fetch_eastmoney                 # 全部 watchlist
    python -m src.ingest.fetch_eastmoney --code 600519   # 单家
    python -m src.ingest.fetch_eastmoney --periods 8
"""
from __future__ import annotations

import json
from pathlib import Path

from src import config, db, net

# ==================== 源 → 取数 ====================

_SOURCE_CACHE: dict[tuple[str, str, int, str | None], list[dict]] = {}


def to_secucode(code: str) -> str:
    """6 位代码 → 东财 SECUCODE（600519.SH / 000858.SZ / 830799.BJ）。

    与巨潮的 `market_of()` 是两套后缀：巨潮用 sse/szse/bj，东财用 SH/SZ/BJ，
    别互相套用（套错的表现是接口返回空，看上去像"没有这家公司"）。
    """
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def needed_fields(source_key: str) -> list[str]:
    """某个源上，口径表里声明过的字段（去重排序）。"""
    out = {field for meta in config.INDICATORS.values()
           for key, field in meta["sources"] if key == source_key}
    return sorted(out)


def fetch_source(source_key: str, code: str, periods: int,
                 report_type: str | None = None) -> list[dict]:
    """拉一个源最近 N 期的数据（带缓存，避免同一源被多个指标重复请求）。

    三个 api 各有各的取数方式，但对上层**同形**（都是 `[{REPORT_DATE, 字段: 值}]`）：
    - `securities`：东财 F10，columns 只取「我们要的字段 + 报告期」不拉 ALL
      （三大报表有 200~320 列，ALL 会让返回体大好几倍而大部分用不上）；
    - `datacenter`：东财数据中心批量报表，客户端筛年报期；
    - `sina`：新浪移动端财报 JSON，转给 fetch_sina（它自己管缓存与错误）。

    新浪分支**不进本模块缓存**：它的参数单位是「报告期条数」而非「年报期数」，
    与这里的 cache_key 语义不同，共用一份缓存会串期数。
    """
    report_type = report_type or config.EM_REPORT_TYPE_ANNUAL
    spec = config.DATA_SOURCES.get(source_key)
    if not spec:
        raise ValueError(f"未知数据源 {source_key}，可选：{list(config.DATA_SOURCES)}")

    if spec["api"] == "sina":
        from src.ingest import fetch_sina
        return fetch_sina.fetch_source(source_key, code, periods, report_type)

    cache_key = (source_key, code, periods, report_type)
    if cache_key in _SOURCE_CACHE:
        return _SOURCE_CACHE[cache_key]

    fields = needed_fields(source_key)
    if not fields:
        _SOURCE_CACHE[cache_key] = []
        return []

    if spec["api"] == "datacenter":
        # 数据中心批量报表：按 SECURITY_CODE 过滤，且**没有报告期类型字段**，
        # 只能客户端按「报告期是 12-31」筛年报（数据源能力所限，见 config 注释）
        url = config.EASTMONEY_DATACENTER_API
        filters = f'(SECURITY_CODE="{code}")'
        # pageSize 要按「一年 4 个季度报」留足：只要 periods 个年报，就得往下多翻 up to 4 倍，
        # 再 +8 兜住"最新一期是季报"的偏移，否则期数会被季度行挤掉、少拿年份。
        params = {"reportName": spec["report_name"],
                  "columns": ",".join(["REPORT_DATE"] + fields),
                  "pageSize": max(periods * 4 + 8, 24), "pageNumber": 1,
                  "filter": filters,
                  "sortColumns": "REPORT_DATE", "sortTypes": -1}
    else:
        url = config.EASTMONEY_SECURITIES_API
        filters = f'(SECUCODE="{to_secucode(code)}")'
        if source_key in config.SOURCES_WITH_REPORT_TYPE:
            filters += f'(REPORT_TYPE="{report_type}")'
        params = {"reportName": spec["report_name"],
                  "columns": ",".join(["REPORT_DATE"] + fields),
                  "pageSize": periods, "pageNumber": 1,
                  "filter": filters,
                  "sortColumns": "REPORT_DATE", "sortTypes": -1}

    data = net.get_json(url, params=params)
    rows = ((data or {}).get("result") or {}).get("data") or []
    if spec["api"] == "datacenter":
        # 客户端筛年报期；顺便截断到请求期数。
        # ⚠️ 这里**不能**对整串直接 endswith("12-31")：REPORT_DATE 形如
        # "2024-12-31 00:00:00"，末尾是时间部分，永远匹配不上 —— 这个坑会让整个
        # 数据中心源静默返回 0 行，表现得像"该股没有数据"，极难排查（保险股 F10 表为空时
        # 全靠它兜底，于是看起来就是"取不到数"）。必须先截到日期部分再比。
        rows = [r for r in rows
                if _period_of(r.get("REPORT_DATE")).endswith(config.ANNUAL_MMDD)][:periods]
    _SOURCE_CACHE[cache_key] = rows
    return rows


def clear_cache() -> None:
    """清掉取数缓存（跑新公司/新期数前调用，避免跨批次串数据）。"""
    _SOURCE_CACHE.clear()
    # 新浪源有自己的缓存与错误账本（见 fetch_sina），必须一起清，
    # 否则上一家的期次说明/源级错误会漏到下一家。
    from src.ingest import fetch_sina
    fetch_sina.clear_cache()


def _period_of(raw_date) -> str:
    return str(raw_date)[:10] if raw_date else ""


def collect_company(code: str, periods: int | None = None,
                    report_type: str | None = None) -> dict:
    """把一家公司的各源数据按口径表整理成 {period -> [指标行]}。

    返回：
    ```
    {"code", "periods": [...], "by_period": {...},
     "missing": [...],          # 在**每一期**都取不到的指标 = 数据源确实没这个字段
     "partial": {period: [...]},# 只在部分期缺失的指标（通常是最早/最新期覆盖不全）
     "sources_used": [...], "source_errors": {...}}
    ```
    row 已带齐入库字段（indicator/value/unit/source_table/source_field）。
    """
    periods = periods or config.EM_PERIODS
    report_type = report_type or config.EM_REPORT_TYPE_ANNUAL

    # 只拉口径表真正引用到的源
    used_keys = sorted({key for meta in config.INDICATORS.values()
                        for key, _ in meta["sources"]})
    rows_by_source = {k: fetch_source(k, code, periods, report_type) for k in used_keys}
    # 转成 {source_key: {period: raw_row}}，便于按指标回溯
    by_source_period: dict[str, dict[str, dict]] = {
        k: {_period_of(r.get("REPORT_DATE")): r for r in v if _period_of(r.get("REPORT_DATE"))}
        for k, v in rows_by_source.items()
    }
    # 源级错误要往上抛，不能"悄悄少了一批数"（新浪单源失败时上面已返回空列表）
    from src.ingest import fetch_sina
    source_errors = fetch_sina.take_errors()

    # 期间轴只由**主源**决定；补充源（新浪）超出主源范围的期直接丢掉，
    # 否则会扩出一个"只有它自己有数"的残缺早期，污染期间序列与缺失统计。
    # 主源全挂时才退化成用所有源（那种情况下面会显式报错，不会静默）。
    primary_periods = sorted(
        {p for k, m in by_source_period.items() if not config.is_supplement_source(k)
         for p in m}, reverse=True)
    if primary_periods:
        all_periods = primary_periods[:periods]
    else:
        all_periods = sorted({p for m in by_source_period.values() for p in m},
                             reverse=True)[:periods]
    period_set = set(all_periods)

    by_period: dict[str, list[dict]] = {p: [] for p in all_periods}
    per_period_missing: dict[str, list[str]] = {p: [] for p in all_periods}
    got_at_least_once: set[str] = set()
    sources_used: set[str] = set()

    for period in all_periods:
        for std, meta in config.INDICATORS.items():
            hit: tuple[str, str, float] | None = None
            for source_key, field in meta["sources"]:
                raw = by_source_period.get(source_key, {}).get(period)
                if not raw or field not in raw:
                    continue
                value = raw.get(field)
                if value is None:
                    continue
                hit = (source_key, field, float(value))
                break
            if hit is None:
                per_period_missing[period].append(std)
                continue
            source_key, field, value = hit
            got_at_least_once.add(std)
            sources_used.add(source_key)
            by_period[period].append({
                "code": code, "period": period,
                "report_type": report_type,
                "indicator": std, "value": value, "unit": meta["unit"],
                "source_table": source_key, "source_field": field,
            })

    # 三种"没取到"要分清，别混成一个 missing：
    #   missing      —— 每一期都没有：数据源确实不提供该字段（如保险股的毛利率）
    #   partial      —— 只在部分期缺：覆盖度问题，换期数/换年可能就有了
    #   source_errors—— 接口本身失败：是故障，不是数据不存在
    # partial 要**排掉 missing 里的指标**，否则同一件事会被报两次（"每期都缺"当然也"部分期缺"），
    # 汇总时看着像两类问题，实际是一类。
    missing = [std for std in config.INDICATORS if std not in got_at_least_once]
    missing_set = set(missing)
    partial = {p: sorted(set(v) - missing_set)
               for p, v in per_period_missing.items()}
    partial = {p: v for p, v in partial.items() if v}

    return {"code": code, "periods": all_periods, "by_period": by_period,
            "missing": sorted(missing), "partial": partial,
            "sources_used": sorted(sources_used), "source_errors": source_errors}


def save_company(result: dict) -> dict:
    """把 collect_company 的结果 upsert 进库，返回统计。"""
    code = result["code"]
    rows = [r for bucket in result["by_period"].values() for r in bucket]
    if not rows:
        return {"code": code, "periods": 0, "rows": 0,
                "missing": result["missing"], "partial": result.get("partial") or {},
                "sources_used": result["sources_used"],
                "source_errors": result.get("source_errors") or {}}

    sql = db.upsert_indicators_sql()
    payload = [(r["code"], r["period"], r["report_type"], r["indicator"], r["value"],
                r["unit"], r["source_table"], r["source_field"]) for r in rows]
    with db.get_conn() as conn:
        conn.executemany(sql, payload)

    return {"code": code, "periods": len(result["periods"]), "rows": len(rows),
            "missing": result["missing"], "partial": result.get("partial") or {},
            "sources_used": result["sources_used"],
            "source_errors": result.get("source_errors") or {}}


# ==================== 配套：公司清单与年报清单 ====================

def sync_companies() -> int:
    """把 watchlist 同步进 companies 表（公司名/市场/orgId 落库，便于工具层展示）。"""
    wl = config.load_watchlist()
    if not wl:
        return 0
    sql = db.upsert_companies_sql()
    payload = [(c.get("code", ""), c.get("name", ""), c.get("market", ""),
                c.get("industry", ""), c.get("org_id", "")) for c in wl if c.get("code")]
    with db.get_conn() as conn:
        conn.executemany(sql, payload)
    return len(payload)


def sync_reports_from_parsed() -> int:
    """把 Step 1 已解析的年报（data/parsed）回填 reports 表。

    为什么回填而不是重解析：解析产物里已有 page_count / empty_pages / section_mode，
    再跑一遍纯属浪费；这一步只是把「文件系统里的既有事实」同步进库，
    让工具层与前端不用再去翻目录。
    """
    sql = db.upsert_report_sql()
    payload: list[tuple] = []
    if config.PARSED_DIR.exists():
        for code_dir in sorted(p for p in config.PARSED_DIR.iterdir() if p.is_dir()):
            for f in sorted(code_dir.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                payload.append((
                    d.get("code"), d.get("year"), "annual",
                    None, None, d.get("source_pdf"), None,
                    d.get("page_count"), d.get("empty_pages"),
                    d.get("section_mode"), None,
                ))
    if not payload:
        return 0
    with db.get_conn() as conn:
        conn.executemany(sql, payload)
    return len(payload)


def fetch_watchlist(codes: list[str] | None = None,
                    periods: int | None = None) -> list[dict]:
    """按 watchlist（或指定代码）批量取数入库。"""
    db.init_schema()
    watch = config.load_watchlist()
    if codes:
        watch = [c for c in watch if c.get("code") in set(codes)] or \
                [{"code": c, "name": c} for c in codes]
    if not watch:
        return []

    out: list[dict] = []
    for c in watch:
        code = c.get("code")
        if not code:
            continue
        clear_cache()               # 每家之间清缓存，避免期数/口径串
        res = collect_company(code, periods=periods)
        stat = save_company(res)
        stat["name"] = c.get("name") or code
        out.append(stat)
    return out


if __name__ == "__main__":
    # 自检：python -m src.ingest.fetch_eastmoney [--code 600519] [--periods 6]
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    args = _sys.argv[1:]
    # 下标必须落到 args 上，不能拿 args 的下标去索引 _sys.argv（会整体错 1 位，
    # 表现是把 "--code" 本身当成股票代码，取数静默返回 0 期 —— 很难看出是参数解析错）
    _codes = [args[i + 1] for i, a in enumerate(args) if a == "--code" and i + 1 < len(args)]
    _periods = int(args[args.index("--periods") + 1]) if "--periods" in args else None

    n_co = sync_companies()
    n_rep = sync_reports_from_parsed()
    print(f"同步 companies {n_co} 家、reports {n_rep} 份")

    stats = fetch_watchlist(codes=_codes or None, periods=_periods)
    print(f"\n取数完成 {len(stats)} 家：")
    for s in stats:
        errs = s.get("source_errors") or {}
        flag = "✗" if errs else ("⚠" if s.get("missing") else "✓")
        print(f"  {flag} {s['code']} {s.get('name', ''):<10} "
              f"{s['periods']} 期 / {s['rows']} 个指标值  "
              f"源={','.join(s['sources_used'])}")
        if errs:
            # 源级失败要显式报出来：这类问题以前表现为"某几项莫名是空的"，极难定位
            print(f"      源级错误：{errs}")
        if s.get("missing"):
            print(f"      每期都取不到（数据源确实不提供该字段）：{s['missing']}")
        if s.get("partial"):
            # 只报"哪几期少"的汇总，不逐期刷屏
            gaps = ", ".join(f"{p}({len(v)})" for p, v in sorted(s["partial"].items()))
            print(f"      部分期缺失（覆盖度问题，非数据源缺陷）：{gaps}")
