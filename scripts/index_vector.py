"""建向量库：chunk → 向量 → `data/vector/`。

用法：
    python scripts/index_vector.py             # 幂等（指纹一致就跳过）
    python scripts/index_vector.py --force     # 强制重建
    python scripts/index_vector.py --status    # 只看现状，不建
    python scripts/index_vector.py --probe     # 先用一句话验证通道，再建（默认开）

为什么要单独一个脚本而不是塞进 `ingest_all.py`：
向量化是**唯一花钱且慢**的一步（265 次 API 调用）。入库流程里其他步骤（下 PDF / 解析 /
切分 / BM25）都是本地且免费的，把它们绑在一起意味着"想重建一次向量就得把整条管线重跑"，
而且失败时很难判断是取数挂了还是向量化挂了。分开之后：
- `ingest_all.py` 保持"离线可全跑"；
- 本脚本独立、幂等、可续跑。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.embedding import EmbeddingUnavailable, describe, embedding_ready  # noqa: E402
from src.ingest import index_vector  # noqa: E402
from src.ingest.chunk import load_all_chunks  # noqa: E402
from src.retrieve import vector as vector_mod  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重建")
    ap.add_argument("--status", action="store_true", help="只打印状态")
    ap.add_argument("--no-probe", action="store_true", help="跳过通道探活")
    args = ap.parse_args()

    print("=" * 68)
    print("向量库状态")
    print("=" * 68)
    print("  通道:", json.dumps(describe(), ensure_ascii=False))
    st = vector_mod.status()
    print("  可用:", json.dumps(st, ensure_ascii=False))

    if args.status:
        return 0

    ready, reason = embedding_ready()
    if not ready:
        print(f"\n✗ 向量通道不可用：{reason}")
        print("  → 检索会**自动降级为纯 BM25**（链路仍可跑，但语义召回缺失）。")
        return 2

    if not args.no_probe:
        t0 = time.monotonic()
        from src.embedding import embed_query
        v = embed_query("贵州茅台2024年的毛利率是多少")
        print(f"\n  探活：查询向量 dim={v.shape[0]}（{time.monotonic() - t0:.2f}s）")

    chunks = load_all_chunks()
    print(f"\nchunk 总数：{len(chunks)}")
    if not chunks:
        print("✗ 没有 chunk，先跑 scripts/ingest_all.py")
        return 2

    print("=" * 68)
    print(f"向量化（模型 {config.EMBEDDING_API_MODEL if config.EMBEDDING_BACKEND == 'api' else config.EMBEDDING_MODEL}"
          f"，batch={config.EMBEDDING_BATCH_SIZE}，"
          f"预计 {(len(chunks) + config.EMBEDDING_BATCH_SIZE - 1) // config.EMBEDDING_BATCH_SIZE} 次请求）")
    print("=" * 68)
    try:
        stat = index_vector.build(chunks, force=args.force)
    except EmbeddingUnavailable as e:
        print(f"\n✗ 向量化失败：{e}")
        print("  → 已写入的断点保留在 data/vector/*.part*，修好后重跑本脚本会**从此处继续**。")
        return 1

    if stat.get("skipped"):
        print("  （未重建）")
    print("\n" + "=" * 68)
    print("复核")
    print("=" * 68)
    vector_mod.reset_index()
    print("  status:", json.dumps(vector_mod.status(), ensure_ascii=False))
    print("  自检查询「毛利率最高的产品」top3：")
    for h in vector_mod.get_index().search("毛利率最高的产品", topk=3):
        print(f"    {h['rank']}. cos={h['cosine']:.4f} {h['company']}{h['year']} "
              f"P{h['page_no']} {(h['section'] or '')[:16]} {h['chunk_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
