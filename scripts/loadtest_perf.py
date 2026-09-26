# -*- coding: utf-8 -*-
"""并发压测：给简历补真实的性能数据（不调大模型，use_llm=false 走离线链路）。

三个场景
  health  GET  /api/health               —— 服务自检（含索引/法规/通道状态计算）
  compare GET  /api/compare              —— 工具层 + SQLite 取数（无 LLM）
  stream  POST /api/ask/stream           —— SSE 全链路（检索 → 引用 → 校验 → 审计），use_llm=false

用法
  .venv/Scripts/python.exe scripts/loadtest_perf.py --scenario all --base-url http://127.0.0.1:8011
  .venv/Scripts/python.exe scripts/loadtest_perf.py --scenario stream --concurrency 10,30 --total 300
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "eval"

HEALTH = "/api/health"
COMPARE = "/api/compare?indicator={ind}&codes={codes}"
STREAM = "/api/ask/stream"

DEFAULT_IND = "营业总收入"
DEFAULT_CODES = "600519,000858"
STREAM_QUESTION = "贵州茅台2024年的毛利率是多少"


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * len(xs) + 0.5)) - 1))
    return xs[k]


async def hit_health(client: httpx.AsyncClient, base: str) -> dict:
    t0 = time.perf_counter()
    r = await client.get(base + HEALTH)
    dt = (time.perf_counter() - t0) * 1000
    return {"ok": r.status_code == 200, "ms": dt, "status": r.status_code, "bytes": len(r.content)}


async def hit_compare(client: httpx.AsyncClient, base: str, ind: str, codes: str) -> dict:
    url = base + COMPARE.format(ind=ind, codes=codes)
    t0 = time.perf_counter()
    r = await client.get(url)
    dt = (time.perf_counter() - t0) * 1000
    return {"ok": r.status_code == 200, "ms": dt, "status": r.status_code, "bytes": len(r.content)}


async def hit_stream(client: httpx.AsyncClient, base: str, question: str) -> dict:
    t0 = time.perf_counter()
    ttfb = None
    events = 0
    bytes_in = 0
    ok = False
    try:
        async with client.stream("POST", base + STREAM,
                                 json={"question": question, "use_llm": False}) as r:
            if r.status_code != 200:
                await r.aread()
                return {"ok": False, "ms": (time.perf_counter() - t0) * 1000,
                        "status": r.status_code, "ttfb": None, "events": 0, "bytes": 0}
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                if ttfb is None:
                    ttfb = (time.perf_counter() - t0) * 1000
                bytes_in += len(line)
                if line.startswith("event:"):
                    events += 1
                    if line.split(":", 1)[1].strip() == "done":
                        ok = True
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "ms": (time.perf_counter() - t0) * 1000, "status": -1,
                "ttfb": None, "events": events, "bytes": bytes_in, "err": type(exc).__name__}
    return {"ok": ok, "ms": (time.perf_counter() - t0) * 1000, "status": 200,
            "ttfb": ttfb, "events": events, "bytes": bytes_in}


SCENARIOS = {
    "health": lambda c, b: hit_health(c, b),
    "compare": lambda c, b: hit_compare(c, b, DEFAULT_IND, DEFAULT_CODES),
    "stream": lambda c, b: hit_stream(c, b, STREAM_QUESTION),
}


async def run_level(scenario: str, base: str, concurrency: int, total: int,
                    timeout: float) -> dict:
    fn = SCENARIOS[scenario]
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency * 2)
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        # 预热，避免把建连成本算进第一档
        for _ in range(min(3, total)):
            await fn(client, base)

        async def worker():
            async with sem:
                results.append(await fn(client, base))

        t0 = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(total)])
        wall = time.perf_counter() - t0

    lat = [r["ms"] for r in results]
    ttfbs = [r["ttfb"] for r in results if r.get("ttfb")]
    ok_n = sum(1 for r in results if r["ok"])
    data = {
        "scenario": scenario,
        "concurrency": concurrency,
        "requests": total,
        "ok": ok_n,
        "err": total - ok_n,
        "wall_s": round(wall, 2),
        "qps": round(total / wall, 1),
        "mean_ms": round(statistics.fmean(lat), 1) if lat else None,
        "p50_ms": round(pct(lat, 50), 1) if lat else None,
        "p95_ms": round(pct(lat, 95), 1) if lat else None,
        "p99_ms": round(pct(lat, 99), 1) if lat else None,
        "max_ms": round(max(lat), 1) if lat else None,
    }
    if ttfbs:
        data["ttfb_p50_ms"] = round(pct(ttfbs, 50), 1)
        data["ttfb_p95_ms"] = round(pct(ttfbs, 95), 1)
        data["events_avg"] = round(statistics.fmean([r["events"] for r in results]), 1)
    return data


def fmt(row: dict) -> str:
    base = (f"{row['scenario']:<8} c={row['concurrency']:<3} n={row['requests']:<5} "
            f"QPS={row['qps']:<7} P50={row['p50_ms']:<7} P95={row['p95_ms']:<7} "
            f"P99={row['p99_ms']:<7} max={row['max_ms']:<7} err={row['err']}")
    if "ttfb_p50_ms" in row:
        base += f" | TTFB50={row['ttfb_p50_ms']} TTFB95={row['ttfb_p95_ms']} events={row['events_avg']}"
    return base


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8011")
    ap.add_argument("--scenario", default="all", choices=["health", "compare", "stream", "all"])
    ap.add_argument("--concurrency", default="1,10,30,50")
    ap.add_argument("--total", type=int, default=0, help="每档总请求数；0=按并发自适应")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--label", default="", help="结果标签，如 auth_off")
    args = ap.parse_args()

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    scenarios = ["health", "compare", "stream"] if args.scenario == "all" else [args.scenario]

    ready = httpx.get(args.base_url + HEALTH, timeout=10)
    print(f"[precheck] {args.base_url}{HEALTH} -> {ready.status_code}")

    rows = []
    for sc in scenarios:
        for c in levels:
            total = args.total or max(60, c * 20)
            row = await run_level(sc, args.base_url, c, total, args.timeout)
            rows.append(row)
            print(fmt(row))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"loadtest_{args.label or 'run'}_{ts}.json"
    out.write_text(json.dumps({"base_url": args.base_url, "label": args.label,
                               "question": STREAM_QUESTION, "indicator": DEFAULT_IND,
                               "codes": DEFAULT_CODES, "use_llm": False, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[saved] {out}")


if __name__ == "__main__":
    asyncio.run(main())
