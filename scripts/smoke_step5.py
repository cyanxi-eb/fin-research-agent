"""Step 5 冒烟 —— 三类问题各走一条子图 + HITL 跨进程证据链。

```
python scripts/smoke_step5.py            # 全部
python scripts/smoke_step5.py --routes   # 只跑三条路由
python scripts/smoke_step5.py --hitl     # 只跑 HITL 双进程链条
```

## 为什么 HITL 这条要用**两个真子进程**

验收标准写的是"进程 A 跑出挂起 → 进程 B 用同一 thread_id 恢复并确认"。
在同一个进程里自证是**不算数**的：内存里存着状态的话，同进程必然读得回来，
用例会稳定地绿，而真实场景（人隔几小时才确认，中间服务重启）恰恰是最需要它的地方。
所以这里用 `subprocess` 起两个独立进程，状态只能来自 `data/db/checkpoints.db`。

另外 HITL 的真实触发条件（低置信度 / 库被并发改写）不能稳定复现，
脚本用 `FA_HITL_FORCE_REASON` 演练开关强制挂起一次 —— 它是**只改结论不改判定**的
开关（见 config 的说明），所以这条链路的验证是真实的。

**与鉴权无关（说清楚，免得被误当成"鉴权也测过了"）**：本脚本是**进程内**调用
（`builder.run_agent` / `agent_status` / `resume_agent`，跨进程那条也只起子进程跑同样的进程内代码），
**不发任何 HTTP 请求**，所以没有请求头可带、也不经过 `current_user` 这个依赖。
鉴权契约由 `tests/test_server_auth.py` 与 `scripts/smoke_step6.py`（带 token 的形态）覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = [
    ("数值题（走工具层）", "五粮液和贵州茅台2024年的毛利率对比", "analysis"),
    ("法规题（查法规库）", "上市公司未在规定期限内披露年度报告会有什么后果", "compliance"),
    ("文档题（检索原文）", "贵州茅台2024年年报的审计机构是哪家", "rag"),
]

_PY = sys.executable


def _env(force_reason: str | None = None) -> dict:
    env = dict(os.environ)
    env.pop("FA_HITL_FORCE_REASON", None)
    if force_reason:
        env["FA_HITL_FORCE_REASON"] = force_reason
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ==================== 三条路由 ====================

def run_routes() -> bool:
    from src.graph import builder

    ok = True
    print("=" * 78)
    print("Step 5 冒烟 · 三条子图（use_llm=False，不联网）")
    print("=" * 78)
    for title, question, expect in CASES:
        r = builder.run_agent(question, use_llm=False, audit=False)
        got = r["intent"]
        flag = "✅" if got == expect else "❌"
        ok &= (got == expect)
        print(f"\n{flag} {title}")
        print(f"   问题     : {question}")
        print(f"   意图     : {got}（期望 {expect}；rule={r['route']['rule']}）")
        print(f"   拒答/挂起: refused={r['refused']} hitl={r['hitl']['pending']}"
              f"（{r['hitl']['reason']}） 置信度={r['confidence']}")
        v = r.get("verify") or {}
        print(f"   校验     : checked={v.get('checked')} supported={v.get('supported')}"
              f" 无出处数字={v.get('unsupported')} 死链={v.get('dangling_citations')}")
        if r["tool_calls"]:
            for c in r["tool_calls"]:
                print(f"   工具     : {c['name']} → {c['summary']}")
        if r["citations"]:
            print(f"   引用({len(r['citations'])}) : " + " | ".join(
                c["citation"] for c in r["citations"][:3]))
        print(f"   答案首行 : {(r['answer'] or '').splitlines()[0][:76]}")
    return ok


# ==================== HITL 双进程 ====================

_A_SCRIPT = r"""
import json, sys
sys.path.insert(0, r"{root}")
from src.graph import builder
r = builder.run_agent("贵州茅台2024年年报的审计机构是哪家", use_llm=False, audit=True)
tid = r["thread_id"]
print(json.dumps({{
    "thread_id": tid, "pending": r["hitl"]["pending"], "reason": r["hitl"]["reason"],
    "answer_chars": len(r["answer"] or ""), "citations": len(r["citations"] or []),
    "hitl_notes": [n for n in r["notes"] if "演练开关" in n],
}}, ensure_ascii=False))
open(r"{tidfile}", "w", encoding="utf-8").write(tid)
"""

_B_SCRIPT = r"""
import json, sys
sys.path.insert(0, r"{root}")
from src.graph import builder
from src import audit
tid = open(r"{tidfile}", encoding="utf-8").read().strip()
st = builder.agent_status(tid)
before = {{
    "found": st["found"], "pending": st["pending"], "next": st["next"],
    "payload_reason": (st.get("payload") or {{}}).get("reason"),
    "payload_has_answer": bool((st.get("payload") or {{}}).get("answer")),
    "payload_has_verify": bool((st.get("payload") or {{}}).get("verify")),
}}
out = builder.resume_agent(tid, {{"decision": "approve", "note": "冒烟：人工核对页码无误",
                               "reviewer": "smoke-bot"}}, audit=True)
after = {{
    "pending": out["hitl"]["pending"], "reviewed": out["hitl"]["reviewed"],
    "decision": (out["hitl"].get("decision") or {{}}).get("decision"),
    "human_note": [n for n in out["notes"] if "人工确认" in n],
}}
print(json.dumps({{"thread_id": tid, "before_resume": before, "after_resume": after,
                  "audit": audit.stats()}}, ensure_ascii=False))
"""


def _run_child(code: str, env: dict) -> dict:
    proc = subprocess.run([_PY, "-c", code], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"子进程失败（exit={proc.returncode}）：\n{proc.stdout}\n{proc.stderr}")
    line = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


def run_hitl() -> bool:
    print("=" * 78)
    print("Step 5 冒烟 · HITL 跨进程证据链（两个独立子进程，状态只可能来自 checkpoints.db）")
    print("=" * 78)
    tid_file = ROOT / "data" / "_smoke_step5_tid.txt"
    env = _env(force_reason="low_confidence")

    print("\n[进程 A] 提问 → 期望挂起")
    a = _run_child(_A_SCRIPT.format(root=str(ROOT), tidfile=str(tid_file)), env)
    print(json.dumps(a, ensure_ascii=False, indent=2))

    print("\n[进程 B] 新进程：读回挂起状态 → 人工确认 → 定稿")
    b = _run_child(_B_SCRIPT.format(root=str(ROOT), tidfile=str(tid_file)), env)
    print(json.dumps(b, ensure_ascii=False, indent=2))

    ok = (a["pending"] is True and a["thread_id"] == b["thread_id"]
          and b["before_resume"]["found"] is True
          and b["before_resume"]["pending"] is True
          and b["before_resume"]["next"] == ["hitl"]
          and b["before_resume"]["payload_has_answer"] is True
          and b["after_resume"]["reviewed"] is True
          and b["after_resume"]["decision"] == "approve"
          and (b["audit"].get("by_action") or {}).get("hitl_confirm", 0) >= 1)
    print(f"\n{'✅' if ok else '❌'} 跨进程挂起→读回→确认 链路{'通过' if ok else '失败'}")
    print(f"   会话键：{a['thread_id']}（两进程共用；状态落盘于 "
          f"data/db/checkpoints.db）")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 5 冒烟")
    ap.add_argument("--routes", action="store_true", help="只跑三条子图")
    ap.add_argument("--hitl", action="store_true", help="只跑 HITL 双进程链")
    args = ap.parse_args()

    run_all = not (args.routes or args.hitl)
    print("Step 5 冒烟 · 进程内跑图（不发 HTTP 请求）：不经过 Bearer 鉴权层，"
          "本结果不代表鉴权路径已被验证（见 tests/test_server_auth.py）。\n")
    results: list[bool] = []
    if run_all or args.routes:
        results.append(run_routes())
    if run_all or args.hitl:
        results.append(run_hitl())

    print("\n" + "=" * 78)
    ok = all(results)
    print(("✅ Step 5 冒烟全部通过" if ok else "❌ Step 5 冒烟有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
