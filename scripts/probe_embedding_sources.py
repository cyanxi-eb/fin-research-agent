"""探针：向量化 / 重排 到底能走哪条路（Step 4 前置，**先探活再实现**）。

为什么必须先探：Step 4 的整套设计（向量召回 + RRF + rerank）都建立在一个前提上 ——
「有一台能跑 embedding 的机器」。而这个前提有两种完全不同的落地方式，成本差一个数量级：

- **API 通道**：零重依赖，只要 Key + 网络。维度、限流、是否支持 batch 都要实测。
- **本地通道**：`sentence-transformers` + torch，模型权重从 HuggingFace 下载。
  国内直连 HF 基本不通，得走镜像；torch 在 Windows 上装 CPU 版也有几百 MB。

本脚本一次把两条路都测出来，输出**可直接抄进 config 的结论**。
不打印任何 Key 明文（只打前缀+尾四位）。

用法：
    python scripts/probe_embedding_sources.py
    python scripts/probe_embedding_sources.py --skip-local   # 只测 API 通道
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import requests  # noqa: E402

from src import config  # noqa: E402


def _mask(key: str) -> str:
    return f"{key[:6]}...{key[-4:]}" if len(key) > 12 else ("(空)" if not key else "(短)")


def _post(url: str, payload: dict, headers: dict, timeout: int = 30) -> tuple[bool, str, dict | None]:
    """发一个 JSON POST，返回 (成功, 人话描述, body)。"""
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return False, f"网络异常: {type(e).__name__}: {str(e)[:120]}", None
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}", None
    try:
        return True, "ok", r.json()
    except json.JSONDecodeError:
        return False, f"返回非 JSON: {r.text[:120]}", None


# ---------------- 通道 1：OpenAI 兼容 /embeddings ----------------

# 常见候选模型名。**不能凭记忆挑一个写进 config** —— 名称写错时服务端多半回
# "model not found"，但有的兼容网关会**静默回落到默认模型**（维度一样，看起来正常），
# 所以下面连维度一起打出来对照。
EMBED_CANDIDATES = ["text-embedding-v4", "text-embedding-v3", "text-embedding-v2"]


def probe_openai_compatible_embeddings(label: str, base_url: str, api_key: str,
                                       models: list[str]) -> dict:
    print(f"\n=== [embedding/api] {label} ===")
    print(f"  base_url : {base_url}")
    print(f"  api_key  : {_mask(api_key)}")
    if not api_key.strip():
        print("  -> 未配置 Key，跳过")
        return {"label": label, "available": False, "reason": "no_key"}

    url = base_url.rstrip("/") + "/embeddings"
    ok_any = False
    result: dict = {"label": label, "available": False, "models": []}
    for m in models:
        t0 = time.monotonic()
        ok, msg, body = _post(url, {"model": m, "input": ["贵州茅台2024年毛利率"]},
                              {"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"})
        dt = time.monotonic() - t0
        if not ok:
            print(f"  [{m:<22}] ✗ {msg}")
            result["models"].append({"model": m, "ok": False, "error": msg})
            continue
        vec = ((body or {}).get("data") or [{}])[0].get("embedding") or []
        usage = (body or {}).get("usage") or {}
        print(f"  [{m:<22}] ✓ 维度={len(vec):<5} 耗时={dt:.2f}s tokens={usage.get('total_tokens')}")
        result["models"].append({"model": m, "ok": True, "dim": len(vec),
                                 "seconds": round(dt, 2), "usage": usage})
        if not ok_any:
            result.update(available=True, model=m, dim=len(vec))
            ok_any = True
    if not ok_any:
        result["reason"] = "all_models_failed"

    # batch 上限探测：向量化 2650 个 chunk 时这是**决定性的**（一条条发要几千次请求）
    if ok_any:
        m = result["model"]
        for n in (10, 25, 100):
            texts = [f"贵州茅台2024年第{i}项财务指标" for i in range(n)]
            ok, msg, body = _post(url, {"model": m, "input": texts},
                                  {"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"})
            if ok:
                got = len((body or {}).get("data") or [])
                print(f"  batch={n:<4} ✓ 返回 {got} 条")
                result["max_batch"] = n
            else:
                print(f"  batch={n:<4} ✗ {msg[:100]}")
                break
    return result


# ---------------- 通道 2：DashScope 原生 rerank ----------------

DASHSCOPE_RERANK_URL = ("https://dashscope.aliyuncs.com/api/v1/services/rerank"
                        "/text-rerank/text-rerank")
RERANK_CANDIDATES = ["gte-rerank-v2", "gte-rerank"]


def probe_dashscope_rerank(api_key: str) -> dict:
    print("\n=== [rerank/api] DashScope text-rerank ===")
    print(f"  api_key  : {_mask(api_key)}")
    if not api_key.strip():
        print("  -> 未配置 Key，跳过")
        return {"available": False, "reason": "no_key"}
    docs = ["贵州茅台2024年毛利率为91.93%", "公司召开董事会审议年度报告", "研发费用同比增加"]
    result: dict = {"available": False, "models": []}
    for m in RERANK_CANDIDATES:
        payload = {"model": m,
                   "input": {"query": "贵州茅台毛利率", "documents": docs},
                   "parameters": {"return_documents": False}}
        ok, msg, body = _post(DASHSCOPE_RERANK_URL, payload,
                              {"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"})
        if not ok:
            print(f"  [{m:<16}] ✗ {msg}")
            result["models"].append({"model": m, "ok": False, "error": msg})
            continue
        results = ((body or {}).get("output") or {}).get("results") or []
        top = results[0] if results else {}
        print(f"  [{m:<16}] ✓ 返回 {len(results)} 条，top index={top.get('index')} "
              f"score={top.get('relevance_score')}")
        result["models"].append({"model": m, "ok": True, "n": len(results),
                                 "top_score": top.get("relevance_score")})
        if not result["available"]:
            result.update(available=True, model=m)
    return result


# ---------------- 通道 3：本地 sentence-transformers ----------------

def probe_local(label: str, model_name: str, mirroed: bool = True) -> dict:
    print(f"\n=== [embedding/local] {label} ===")
    print(f"  model    : {model_name}")
    try:
        import sentence_transformers  # noqa: F401
        print("  依赖     : sentence-transformers 已安装")
    except ImportError:
        print("  依赖     : ✗ 未安装 sentence-transformers（需 pip install sentence-transformers）")
        return {"label": label, "available": False, "reason": "dep_missing"}

    # 镜像连通性：国内直连 huggingface.co 基本不通，hf-mirror.com 是常用替代
    for url in ("https://huggingface.co", "https://hf-mirror.com"):
        try:
            r = requests.head(url, timeout=8, allow_redirects=True)
            print(f"  连通性   : {url} -> HTTP {r.status_code}")
        except requests.RequestException as e:
            print(f"  连通性   : {url} -> ✗ {type(e).__name__}")

    t0 = time.monotonic()
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(model_name)
        vec = model.encode(["贵州茅台2024年毛利率"], normalize_embeddings=True)[0]
        print(f"  -> ✓ 加载并编码成功，维度={len(vec)}，耗时={time.monotonic() - t0:.1f}s")
        return {"label": label, "available": True, "model": model_name, "dim": len(vec)}
    except Exception as e:  # 下载失败 / 加载失败 / OOM，一律当"不可用"
        print(f"  -> ✗ {type(e).__name__}: {str(e)[:200]}")
        return {"label": label, "available": False, "reason": f"{type(e).__name__}",
                "error": str(e)[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-local", action="store_true", help="只测 API 通道")
    ap.add_argument("--out", default=str(config.DATA_DIR / "probe_embedding.json"))
    args = ap.parse_args()

    out: dict = {"probed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "channels": []}

    # 1. 通义千问（DashScope 兼容模式）—— 本项目已配 Key，首选
    qwen = config.LLM_PROVIDERS["qwen"]
    out["channels"].append(
        probe_openai_compatible_embeddings("qwen/DashScope", qwen["base_url"], qwen["api_key"],
                                           EMBED_CANDIDATES))
    out["channels"].append(probe_dashscope_rerank(qwen["api_key"]))

    # 2. DeepSeek —— 官方**不提供** embeddings 接口，但实测一下比"凭记忆断言没有"可靠
    ds = config.LLM_PROVIDERS["deepseek"]
    out["channels"].append(
        probe_openai_compatible_embeddings("deepseek", ds["base_url"], ds["api_key"],
                                           ["deepseek-embedding"]))

    # 3. 本地通道（可选依赖）
    if not args.skip_local:
        out["channels"].append(
            probe_local("本地 bge-small-zh", config.EMBEDDING_MODEL))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结论已落盘：{args.out}")
    print("\n各通道可用性汇总：")
    for c in out["channels"]:
        print(f"  {c.get('label', '-'):<18} available={c.get('available')} "
              f"{'model=' + str(c.get('model')) if c.get('model') else c.get('reason', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
