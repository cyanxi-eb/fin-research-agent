"""构建评测集：把 `eval/golden_qa.jsonl` 里的题目**解析成可核对的期望页码**。

## 为什么需要"构建"这一步，而不是手写期望页码

因为**手写页码几乎一定错**，而错的金标准会让整份评测数字失去意义（还算不出来错在哪）。
这里把期望页码的来源定成**源 PDF 解析产物**：给一个"标记串"（某个只在该页出现的数值/短语），
在 `data/parsed/<code>/<year>.json` 里全页扫描，命中的页就是期望页。

三个好处：
1. **可复核**：任何一行都能用 `--grep` 回到原文那一页看一遍；
2. **可发现错误**：标记串扫不到任何页 → 直接失败退出（说明我编了一个原文里没有的值），
   而不是安静地写一个空期望页让指标虚高；
3. **与检索结果无关**：期望页来自原文，不是从"检索命中了哪里"反推出来的 ——
   后者是典型的测试泄漏（用结果定义正确答案，指标必然漂亮）。

## 标记串怎么选

- **指标类**（`gt.type=indicator`）：从结构化库里取该指标的值，自动生成几种书写形式
  （元/亿元/千分位/百分数小数位），**选命中页数最少的那个**当标记 —— 命中页越少说明它越独特，
  作为"这一页才有"的证据越强。命中页数过多（> `BROAD_PAGES`）会被标记 `broad`，
  评测时单独统计（因为"答案数字满篇都是"的题，命中率高是理所当然的，不该和别的一起平均）。
- **原文类**（`gt.type=literal`）：人工在 jsonl 里写短语，本脚本只做**验证**。
- **法规类**（`gt.type=article`）：期望是"法规索引里的哪一条"（`doc_no` + `article_no`），
  不是年报页码 —— 见 `resolve_article()`。

用法：
    python scripts/build_golden.py                    # 解析并回写 expected_pages
    python scripts/build_golden.py --dry-run          # 只看不写
    python scripts/build_golden.py --stats            # 汇总
    python scripts/build_golden.py --grep "91.93" --code 600519 --year 2024   # 手工排查
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.retrieve.bm25 import normalize_for_match  # noqa: E402

GOLDEN_PATH: Path = config.ROOT_DIR / "eval" / "golden_qa.jsonl"
# 命中页数超过它就算"到处都是"，评测时单独归类
BROAD_PAGES = 5
# 标记串太短就没有区分度（"9.7" 满篇都是）
MIN_MARKER_CHARS = 4


def norm(s: str) -> str:
    """归一化：去空白 + 去千分位逗号。

    去空白**必须有** —— PDF 抽出来的表格常常把数字从中间断开（`91.9\\n3`），
    这与 `bm25.normalize_for_match` 是同一个理由（那里也是为短语命中做同样的事）。
    去逗号是为了让"1,741.44"与"1741.44"两种写法互相匹配。
    """
    return re.sub(r"[\s,，]", "", (s or "")).lower()


_PAGE_CACHE: dict[tuple[str, int], dict[int, str]] = {}


def load_pages(code: str, year: int) -> dict[int, str]:
    key = (code, year)
    if key not in _PAGE_CACHE:
        path = config.PARSED_DIR / code / f"{year}.json"
        if not path.exists():
            _PAGE_CACHE[key] = {}
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            _PAGE_CACHE[key] = {int(p["page_no"]): norm(p.get("text", ""))
                                for p in data.get("pages", [])}
    return _PAGE_CACHE[key]


def pages_with(code: str, year: int, marker: str) -> list[int]:
    m = norm(marker)
    if len(m) < MIN_MARKER_CHARS:
        return []
    return sorted(p for p, text in load_pages(code, year).items() if m in text)


def indicator_value(code: str, year: int, indicator: str) -> tuple[float, str] | None:
    """从结构化库取该指标的**年报**值（唯一事实来源，不重新取数）。"""
    import sqlite3
    if not config.DB_PATH.exists():
        return None
    con = sqlite3.connect(config.DB_PATH)
    try:
        row = con.execute(
            "select value, unit from financial_indicators where code=? and indicator=? "
            "and report_type='年报' and period like ?",
            (code, indicator, f"{year}%")).fetchone()
    finally:
        con.close()
    return (float(row[0]), row[1] or "") if row and row[0] is not None else None


def candidates(value: float, unit: str) -> list[str]:
    """一个数值在年报里可能的几种写法。

    **单位必须穷举**：这一步踩过坑 —— 宁德时代整本报**千元**、中国平安报**百万元**、
    制造业年报报**元**。只按"元/亿元"两种写法去扫，会出现"原文里明明有这个数却扫不到"，
    进而误判成"数据源给错了"。所以这里把 元/千元/万元/百万元/亿元 全列上，
    让"命中页数最少"的规则去挑真正对得上的那种。

    注意：**不是所有数值都能在原文里找到**。财务报表表格经 PDF 抽取后，
    单元格数字常被交错拼接（实测 600519 现金流量表页把相邻列的数字串在一起），
    这时任何写法都扫不到 —— 属于抽取能力的边界，脚本会如实报"构建失败"，
    而不是编一个页码糊过去（见 `resolve()` 的失败分支）。
    """
    if unit == "%":
        return [f"{value:.4f}", f"{value:.2f}", f"{value:.1f}"]
    if abs(value) >= 1e8:      # 金额
        return [f"{value:,.2f}", f"{value:,.0f}",
                f"{value / 1e9:,.4f}", f"{value / 1e9:,.2f}",      # 十亿（少见但平安附表用过）
                f"{value / 1e8:,.4f}", f"{value / 1e8:,.2f}",      # 亿元
                f"{value / 1e6:,.2f}", f"{value / 1e6:,.0f}",      # 百万元（中国平安）
                f"{value / 1e4:,.2f}", f"{value / 1e4:,.0f}",      # 万元
                f"{value / 1e3:,.2f}", f"{value / 1e3:,.0f}"]      # 千元（宁德时代）
    return [f"{value:.4f}", f"{value:.2f}", f"{value:,.2f}"]   # 每股类小额


def _add_note(note: str | None, text: str) -> str:
    """追加说明，**幂等**：同一段说明不会因为重跑而重复堆叠。

    本脚本会反复回写 jsonl，而 note 是"从当前行内容继续追加"的 —— 没有这道闸门时，
    宽泛题/兜底题的说明每跑一次就多一份（实测 `ind-601318-2024-assets` 已叠了两份）。
    """
    note = note or ""
    return note if text in note else note + text


def resolve_article(row: dict, gt: dict) -> dict:
    """法规题：期望**不是"年报第几页"**，而是"法规索引里的哪一条"。

    法规题问的是条文本身（如"年报最晚什么时候披露完"），它不属于任何一家公司的年报，
    所以**不能**套用页码金标准 —— 硬套的话要么编一个页码，要么把整部办法当成一页，
    两者都会让指标失真。这里的做法是：拿 `doc_no` + `article_no` 去法规索引里核对该条
    真的存在，命中即 `verified`，`expected_pages` 留空（本题不进页级/章节级分母）。

    **判定以 `article_no` 为准**（`article_label` 只作为人读的补充，不参与匹配）：
    条号是稳定的键，而"第十三条"这类中文标签在两版办法里可能因为增删条而错位。
    核对不到就 `verified=False`（脚本因此打印 ✗ 并非零退出）—— 不许编一条不存在的法规。
    """
    out = dict(row)
    doc_no = gt.get("doc_no")
    art_no = str(gt.get("article_no") or gt.get("article") or "")
    base = dict(markers=[], expected_pages=[], expected_sections=[], pages_found=0,
                broad=False, gt_page="article")
    try:
        from src.retrieve import regulation
        chunks = regulation.index().chunks
    except Exception as e:  # noqa: BLE001 —— 索引缺失/损坏都该如实报"核不了"，而不是猜
        out.update(**base, verified=False,
                   note=_add_note(row.get("note"),
                                  f" [法规索引不可用：{type(e).__name__}: {e}]"))
        return out
    hit = next((c for c in chunks
                if c.get("doc_no") == doc_no and str(c.get("article_no")) == art_no), None)
    if hit is None:
        out.update(**base, verified=False,
                   note=_add_note(row.get("note"),
                                  f" [法规索引里没有 {doc_no} 第{art_no}条]"))
        return out
    out.update(**base, verified=True,
               gt={**gt, "article_no": art_no,
                   "article_label": hit.get("article_label"),
                   "citation": hit.get("citation"), "status": hit.get("status")},
               note=_add_note(row.get("note"),
                              f" [法规校验：命中 {hit.get('citation')}"
                              f"（{hit.get('status')}）]"))
    return out


def _fallback_section(row: dict, gt: dict, why: str, extra: dict | None = None) -> dict:
    """数值定位失败时的**章节级兜底**。

    为什么要有兜底而不是把这一行删掉：删掉就等于"只评测那些我能定页的题"，
    而能定页的恰恰是原文里以散文/规整表格出现的数 —— 这会系统性地高估检索质量。
    章节级期望（"答案应该在财务报告那几十页里"）比页级弱，但**远强于没有期望**，
    而且它是**可核验的**（章节标签来自 Step 3 修好的 section_marks）。
    评测时页级与章节级分开统计，不混成一个数字。
    """
    secs = gt.get("section") or []
    out = dict(row)
    out.update(markers=[], expected_pages=[], pages_found=0, broad=False,
               expected_sections=list(secs), gt_page="section" if secs else "none",
               verified=bool(secs),
               note=_add_note(row.get("note"), f" [页级定位失败：{why}；"
                              + (f"降级为章节级期望 {secs}]" if secs else "且未给章节期望]")))
    if extra:
        out["gt"] = {**gt, **extra}
    return out


def resolve(row: dict) -> dict:
    """给一行题目补上 `markers / expected_pages / verified / pages_found / gt_page`。"""
    code, year = row.get("code"), row.get("year")
    gt = row.get("gt") or {}
    out = dict(row)
    if row.get("expect_refuse") or gt.get("type") == "none":
        out.update(markers=[], expected_pages=[], pages_found=0, verified=True,
                   broad=False, expected_sections=[], gt_page="none")
        return out
    # 法规题必须在 code/year 检查**之前**分流：它问的是条文，本来就没有公司/年份
    if gt.get("type") == "article":
        return resolve_article(row, gt)
    if not code or not year:
        return _fallback_section(row, gt, "缺少 code/year")
    if gt.get("type") == "lit" or gt.get("type") == "literal":
        mks = [m for m in (row.get("markers") or []) if pages_with(code, year, m)]
        if not mks:
            return _fallback_section(row, gt, "人工标记串未命中任何页")
        hits = sorted({p for m in mks for p in pages_with(code, year, m)})
        out.update(markers=mks, expected_pages=hits, pages_found=len(hits),
                   broad=len(hits) > BROAD_PAGES, verified=True, expected_sections=[],
                   gt_page="page")
        return out

    # indicator：从结构化库取值 → 穷举写法 → 取"命中页数最少"的那种
    got = indicator_value(code, year, gt["indicator"])
    if got is None:
        return _fallback_section(row, gt, f"库里没有 {code} {year} 的「{gt['indicator']}」值")
    value, unit = got
    scored: list[tuple[int, int, str, list[int]]] = []
    for cand in candidates(value, unit):
        hits = pages_with(code, year, cand)
        if hits:
            scored.append((len(hits), -len(norm(cand)), cand, hits))
    if not scored:
        return _fallback_section(row, gt, f"{value}{unit} 的任何写法都没在原文里找到",
                                 extra={"value": value, "unit": unit})
    scored.sort()
    n, _, marker, hits = scored[0]
    out.update(markers=[marker], expected_pages=hits, pages_found=n,
               broad=n > BROAD_PAGES, verified=True, expected_sections=[],
               gt_page="page", gt={**gt, "value": value, "unit": unit})
    if n > BROAD_PAGES:
        out["note"] = _add_note(row.get("note"), f" [标记串命中 {n} 页，属宽泛题]")
    return out


def read_rows() -> list[dict]:
    if not GOLDEN_PATH.exists():
        raise SystemExit(f"评测集不存在：{GOLDEN_PATH}")
    rows = []
    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_rows(rows: list[dict]) -> None:
    GOLDEN_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def do_grep(pattern: str, code: str | None, year: int | None, ctx: int = 60) -> int:
    """手工排查：某个串出现在哪些页（带上下文，便于肉眼确认这页确实是答案所在）。"""
    codes = [code] if code else sorted(p.name for p in config.PARSED_DIR.glob("*") if p.is_dir())
    total = 0
    for c in codes:
        for f in sorted((config.PARSED_DIR / c).glob("*.json")):
            y = int(f.stem)
            if year and y != year:
                continue
            pages = load_pages(c, y)
            for p, text in sorted(pages.items()):
                m = norm(pattern)
                if m and m in text:
                    i = text.index(m)
                    total += 1
                    print(f"{c}/{y} P{p}: ...{text[max(0, i - ctx):i + ctx + len(m)]}...")
    print(f"\n共命中 {total} 处")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--grep")
    ap.add_argument("--code")
    ap.add_argument("--year", type=int)
    args = ap.parse_args()

    if args.grep:
        return do_grep(args.grep, args.code, args.year)

    rows = read_rows()
    resolved = [resolve(r) for r in rows]

    if not args.dry_run and not args.stats:
        write_rows(resolved)
        print(f"已回写 {GOLDEN_PATH}")

    n_ok = sum(1 for r in resolved if r.get("verified"))
    n_bad = len(resolved) - n_ok
    n_broad = sum(1 for r in resolved if r.get("broad"))
    n_ref = sum(1 for r in resolved if r.get("expect_refuse"))
    print(f"\n共 {len(resolved)} 题：已验证 {n_ok} / 失败 {n_bad} / 宽泛 {n_broad} / 拒答题 {n_ref}")
    print(f"{'qa_id':<28} {'公司':<8} {'年':<6} {'期望页':<28} 标记串")
    for r in resolved:
        flag = "" if r.get("verified") else "  ✗"
        pages = ",".join(str(p) for p in r.get("expected_pages", []))[:26]
        print(f"{r['qa_id']:<28} {str(r.get('code')):<8} {str(r.get('year')):<6} "
              f"{pages:<28} {','.join(r.get('markers') or [])}{flag}")
    for r in resolved:
        if not r.get("verified"):
            print(f"\n✗ {r['qa_id']}: {r.get('note')}")
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
