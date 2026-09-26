"""新浪财经财报取数 —— 补齐东财**不提供**的科目。

为什么需要它（不是"多找个备份源"，而是"唯一能拿到某个数的路"）：
东财全系都没有保险股的「归属于母公司股东权益」——
- F10 资产负债表对 601318 整表返回空；
- 数据中心资产负债简表是**金融业业务口径简表**，没有 PARENT_EQUITY 列；
- 主要指标只有 `TOTAL_EQUITY_PK`，那是**含少数股东**的所有者权益合计，是另一个口径。

而券商 App 上中国平安明明有这一项 → 数据客观存在，只是东财不提供。
新浪财经的资产负债表 JSON（表代号 **`fzb`**）有「归属于母公司的股东权益合计」，
实测 601318：归母 9,286.00 亿 + 少数股东 3,761.12 亿 = 13,047.12 亿，
与东财 `TOTAL_EQUITY_PK` **完全一致（恒等式零误差）**；总资产/营业收入也逐项对上。

三个实测要点（都踩过）：
1. **表代号是 `fzb` 不是 `zcfz`**。传 `source=zcfz` 会返回
   `{"status":{"code":0},"data":null}` —— HTTP 200、状态码 0、就是没数据，
   看起来像"这只股票没有资产负债表"，实际只是代号写错。利润表是 `lrb`。
2. **单位是元**（`928600000000` = 9,286 亿），与东财一致，不需要换算。
3. **期次说明是中文文本**（`2024年报` / `2025半年报`），要靠它筛报告期类型；
   不能用"日期是 12-31"这种猜法（那会把季报的期次也混进来）。
   认不出说明文本的期次**直接丢掉**，不猜类型。

⚠️ 新浪没有独立的「营业成本」科目在保险口径下（它的成本行叫「营业支出」），
所以 `营业成本` 对保险仍然缺——这是**报表现实**，不是抓取缺陷。
"""
from __future__ import annotations

from src import config, net

# 一次要多少期：新浪按「报告期」倒序返回，一期只含一张表，
# 想拿 N 个年报就得往下多翻（一年 4 个报告期），故乘 5 再兜底 20。
_PERIOD_MULTIPLIER = 5
_MIN_FETCH = 20

_CACHE: dict[tuple[str, str, int], list[dict]] = {}
_ERRORS: dict[str, str] = {}


def to_paper_code(code: str) -> str:
    """6 位代码 → 新浪 paperCode（sh601318 / sz000858 / bj830799）。

    与巨潮的 market_of（sse/szse/bj）、东财的 to_secucode（SH/SZ/BJ）**是三套后缀**，
    别互相套用 —— 套错的表现是接口返回空，看着像"没有这家公司"。
    """
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def fetch_source(source_key: str, code: str, periods: int,
                 report_type: str | None = None) -> list[dict]:
    """拉一个新浪报表的最近 N 期，返回与东财源**同形**的 `[{REPORT_DATE, 项目名: 值}]`。

    同形是关键：上层 `collect_company` 用 `raw.get(field)` 统一取值，
    于是「东财英文字段名」和「新浪中文项目名」在口径表里写法一致，取数层不需要分支。
    """
    spec = config.DATA_SOURCES.get(source_key)
    if not spec or spec.get("api") != "sina":
        raise ValueError(f"{source_key} 不是新浪源，可选："
                         f"{[k for k, v in config.DATA_SOURCES.items() if v.get('api') == 'sina']}")

    rt = config.normalize_report_type(report_type)
    num = max(periods * _PERIOD_MULTIPLIER, _MIN_FETCH)
    cache_key = (source_key, code, num)
    if cache_key in _CACHE:
        rows = _CACHE[cache_key]
    else:
        params = {"paperCode": to_paper_code(code), "source": spec["source"],
                  "type": "0", "page": "1", "num": str(num)}
        try:
            payload = net.get_json(config.SINA_REPORT_API, params=params,
                                   referer=config.SINA_REFERER)
        except Exception as exc:                       # noqa: BLE001
            # 单个源失败不能拖垮整家公司：记下来、返回空，由上层把"哪一步失败"暴露出去。
            # 这里刻意不静默：_ERRORS 会被 collect_company 带进结果。
            _ERRORS[source_key] = f"{type(exc).__name__}: {exc}"
            return []

        data = ((payload or {}).get("result") or {}).get("data") or {}
        desc: dict[str, str] = {
            str(d.get("date_value")): str(d.get("date_description") or "")
            for d in (data.get("report_date") or [])
        }
        rows = []
        for date_key, bucket in (data.get("report_list") or {}).items():
            iso = _iso_date(str(date_key))
            if not iso:
                continue
            row: dict = {"REPORT_DATE": iso,
                         "_PERIOD_DESC": desc.get(str(date_key), "")}
            for item in ((bucket or {}).get("data") or []):
                title = str(item.get("item_title") or "").strip()
                value = item.get("item_value")
                if title and value is not None:
                    try:
                        row[title] = float(value)
                    except (TypeError, ValueError):
                        continue     # 非数值项（如带说明文字的行）直接丢，不塞进数值表
            rows.append(row)
        rows.sort(key=lambda r: r["REPORT_DATE"], reverse=True)
        _CACHE[cache_key] = rows

    # 按期次说明筛报告期类型；认不出的期次丢掉（不猜）
    return [r for r in rows
            if config.report_type_of_description(r.get("_PERIOD_DESC", "")) == rt]


def _iso_date(date_key: str) -> str | None:
    """`20241231` → `2024-12-31`；非法长度返回 None。"""
    s = (date_key or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def clear_cache() -> None:
    """清缓存（换公司/换期数前调用，避免跨批次串数据）。"""
    _CACHE.clear()
    _ERRORS.clear()


def take_errors() -> dict[str, str]:
    """取走并清空本批次的源级错误（供上层打印/落库，避免"悄悄少了一批数"）。"""
    errs = dict(_ERRORS)
    _ERRORS.clear()
    return errs
