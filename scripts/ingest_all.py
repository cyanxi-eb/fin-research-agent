"""一键入库：取原文 → 按页解析 → 切分 → 建 BM25 索引。

用法：
    python scripts/ingest_all.py                        # 全量（按 watchlist）
    python scripts/ingest_all.py --code 600519          # 只做一家
    python scripts/ingest_all.py --code 600519 --years 2024,2025
    python scripts/ingest_all.py --skip-fetch           # 复用已下载的 PDF
    python scripts/ingest_all.py --force                # 全部重做（重下+重解析）
    python scripts/ingest_all.py --no-index             # 只入库，不重建索引

幂等：已下载的 PDF 按字节数校验跳过；已解析的 json 比 PDF 新则跳过解析。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.ingest import chunk as chunk_mod  # noqa: E402
from src.ingest import fetch_cninfo, parse_pdf  # noqa: E402
from src.retrieve.bm25 import BM25Index  # noqa: E402


def _parse_years(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(y.strip()) for y in raw.split(",") if y.strip()]


def do_fetch(codes: list[str] | None, years: list[int], force: bool) -> list[dict]:
    print("=" * 68)
    print("[1/4] 取原文（巨潮年报 PDF）")
    print("=" * 68)
    results = []
    for c in config.load_watchlist():
        code = str(c.get("code") or "").strip()
        if not code or (codes and code not in codes):
            continue
        r = fetch_cninfo.download_reports(
            code,
            years or list(c.get("years") or []),
            org_id=str(c.get("org_id") or "").strip() or None,
            name=str(c.get("name") or ""),
            market=str(c.get("market") or "").strip() or None,
            force=force,
        )
        results.append(r)
        if not r["ok"] and r.get("error"):
            print(f"  ✗ {code} {r.get('name', '')}: {r['error']}")
            continue
        print(f"  ✓ {code} {r.get('name', ''):<10} orgId={r.get('org_id')} "
              f"市场={r.get('market')} 新下 {r['downloaded']} 跳过 {r['skipped']}"
              + (f" 失败 {r['failed']}" if r.get("failed") else ""))
    return results


def do_parse(codes: list[str] | None, years: list[int], force: bool) -> list[dict]:
    print()
    print("=" * 68)
    print("[2/4] 按页解析（保留页码与章节）")
    print("=" * 68)
    stats = []
    for c in config.load_watchlist():
        code = str(c.get("code") or "").strip()
        if not code or (codes and code not in codes):
            continue
        manifest = fetch_cninfo.load_manifest(code)
        for year_key, entry in sorted(manifest.items()):
            if year_key.startswith("_"):
                continue
            year = int(year_key)
            if years and year not in years:
                continue
            pdf = Path(entry.get("local") or "")
            out = parse_pdf.parsed_path(code, year)
            if not pdf.exists():
                print(f"  · {code}/{year} 跳过：PDF 不存在（{pdf}）")
                continue
            if out.exists() and not force and out.stat().st_mtime >= pdf.stat().st_mtime:
                print(f"  · {code}/{year} 跳过：解析产物已是最新")
                continue
            t0 = time.monotonic()
            p = parse_pdf.parse_pdf(pdf, code, str(c.get("name") or code), year, out)
            print(f"  ✓ {code}/{year} {p['page_count']} 页 "
                  f"空页 {p['empty_pages']} 识别章节 {len(p['section_pages'])} 段 "
                  f"({time.monotonic() - t0:.1f}s)")
            stats.append(p)
    return stats


def do_chunk(codes: list[str] | None, years: list[int], force: bool) -> list[dict]:
    print()
    print("=" * 68)
    print("[3/4] 切分 + 元数据")
    print("=" * 68)
    stats = []
    for f in sorted(config.PARSED_DIR.glob("*/*.json")):
        code, year = f.parent.name, int(f.stem)
        if codes and code not in codes:
            continue
        if years and year not in years:
            continue
        out = chunk_mod.chunks_path(code, year)
        if out.exists() and not force and out.stat().st_mtime >= f.stat().st_mtime:
            print(f"  · {code}/{year} 跳过：chunk 已是最新")
            continue
        import json
        parsed = json.loads(f.read_text(encoding="utf-8"))
        s = chunk_mod.build_from_parsed(parsed)
        stats.append(s)
        print(f"  ✓ {code}/{year} {s['pages']} 页 → {s['chunks']} chunks "
              f"(平均 {s['avg_chars']} 字)")
    return stats


def do_index() -> dict:
    print()
    print("=" * 68)
    print("[4/4] 重建 BM25 索引")
    print("=" * 68)
    t0 = time.monotonic()
    idx = BM25Index.build_from_disk()
    path = idx.save()
    st = idx.stats()
    print(f"  ✓ {st['chunks']} chunks / {st['companies']} 家公司 / "
          f"{st['company_years']} 个公司-年度 / 平均 {st['avg_tokens']} tokens")
    print(f"  → {path}  ({time.monotonic() - t0:.1f}s)")
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", help="只处理指定公司，逗号分隔")
    ap.add_argument("--years", help="只处理指定年份，逗号分隔")
    ap.add_argument("--force", action="store_true", help="全部重做")
    ap.add_argument("--skip-fetch", action="store_true", help="跳过取数")
    ap.add_argument("--no-index", action="store_true", help="不重建索引")
    args = ap.parse_args()

    codes = [c.strip() for c in args.code.split(",")] if args.code else None
    years = _parse_years(args.years)
    t_all = time.monotonic()

    if not args.skip_fetch:
        do_fetch(codes, years, args.force)
    do_parse(codes, years, args.force)
    do_chunk(codes, years, args.force)
    if not args.no_index:
        do_index()

    print()
    print(f"完成，共 {time.monotonic() - t_all:.1f}s")
    print("下一步自检： python -m src.retrieve.bm25 \"毛利率\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
