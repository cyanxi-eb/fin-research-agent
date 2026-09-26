"""Step 6 冒烟 —— 前端托管 + API 契约（`fastapi.testclient`，不起真服务、不花 token）。

```
# 免鉴权形态（FA_AUTH_ENABLED=0）：四条契约全跑，不需要凭据
python scripts/smoke_step6.py

# 鉴权形态：用凭据换票，走完整鉴权路径
$env:FA_AUTH_ENABLED="1"; $env:FA_JWT_SECRET="…"; `
$env:FA_SMOKE_USER="admin"; $env:FA_SMOKE_PASSWORD="…"
python scripts/smoke_step6.py

# 已经有票了（例如刚跑过 scripts/make_token.py）
python scripts/smoke_step6.py --token "<access_token>"
```

## 为什么用 `TestClient` 而不是起一个真 uvicorn

这一步要证的是**应用内部的路由与响应契约**（`/` 是否回 HTML、`/api/health` 是否报
`frontend`、`/api/compare` 是否给出两行、`/api/ask/stream` 首个事件是否为 `meta`）。
这些在进程内就能完全覆盖，起真服务只会引入端口占用、启动竞态与网络不确定性 ——
冒烟脚本应当只失败于**被测代码**，不失败于环境。

## 鉴权：**不配凭据就不许报"通过"**

`with TestClient(app)` 会触发 lifespan（这是它与裸 `TestClient(app)` 的关键差别：
后者不跑启动钩子，于是"配了鉴权却没密钥"这种**部署级错误会被悄悄跳过**）。
启动钩子里有两条硬逻辑：`AUTH_ENABLED=1` 但没有 `JWT_SECRET` → **应用直接起不来**；
有密钥则幂等建演示账号。

既然后端默认开鉴权，冒烟就必须回答"票从哪来"：

| 情况 | 行为 | 退出码 |
|---|---|---|
| `FA_AUTH_ENABLED=0` | 免鉴权形态，四条契约全跑；**明确打印**未覆盖鉴权路径 | 0（全过时） |
| 鉴权开 + 有凭据（或 `--token`）| 登录取票，业务端点带 `Authorization: Bearer`，另加一条"无票必 401"的闸门断言 | 0（全过时） |
| 鉴权开 + 没凭据 | **跳过鉴权端点，仅跑公开端点**，并给出非 0 退出码 | 1 |
| 应用起不来（如缺 `JWT_SECRET`）| 原样打印启动异常 | 2 |

最后一行是这张表存在的理由：**静默降级成"冒烟通过"比崩溃更危险** ——
它会把"根本没测到业务端点"报告成"业务端点正常"。

提问统一 `use_llm=False`：**不联网、不花 token**，走的是确定性降级路径，
`meta` 事件（以及其后的 `done`）仍会照常下发，足以验证 SSE 协议首帧。

失败时**打印实际响应体**：只喊一句「断言失败」等于把排查成本转嫁给下一个人。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.server import app  # noqa: E402


def _snip(text: str, limit: int = 800) -> str:
    """响应体摘录：过长只截前 limit 字符，避免刷屏（但仍能看到真实内容）。"""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字符）"


def first_sse_event(body: str) -> str | None:
    """从 SSE 文本里取**首个** `event:` 行的事件名。"""
    for line in (body or "").split("\n"):
        if line.startswith("event:"):
            return line[len("event:"):].strip()
    return None


def _login(client: TestClient, token_arg: str | None) -> tuple[str | None, str]:
    """拿票：`--token` 优先，其次用 `FA_SMOKE_USER`/`FA_SMOKE_PASSWORD` 登录。

    返回 `(token, 说明)`；token 为 None 时说明写清**为什么没拿到**（要原样打给操作者，
    不能只说"跳过"）。
    """
    if token_arg:
        return token_arg, "--token 直接给出"

    user = (os.getenv("FA_SMOKE_USER") or "").strip()
    pwd = os.getenv("FA_SMOKE_PASSWORD") or ""
    if not user or not pwd:
        return None, ("未提供凭据：请设 FA_SMOKE_USER/FA_SMOKE_PASSWORD，"
                      "或 --token，或把 FA_AUTH_ENABLED 设为 0")

    r = client.post("/api/auth/login", json={"username": user, "password": pwd})
    if r.status_code != 200:
        return None, f"登录失败（POST /api/auth/login → {r.status_code}）：{_snip(r.text, 200)}"
    return r.json().get("access_token"), f"以 {user} 登录取得"


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 6 冒烟（TestClient）")
    ap.add_argument("--token", help="直接给一个 access token（省掉登录这一步）")
    args = ap.parse_args()

    try:
        with TestClient(app) as client:      # with：触发 lifespan（启动自检 + 建演示账号）
            return _run(client, args)
    except RuntimeError as e:
        # lifespan 里的硬失败（最典型：AUTH_ENABLED=1 但没配 JWT_SECRET）。原样打印，
        # 因为这句话本身就是给运维看的部署提示（见 auth.require_jwt_secret）。
        print("\n❌ 应用启动失败（lifespan 抛错）：")
        print(f"   {e}")
        return 2


def _run(client: TestClient, args) -> int:
    results: list[bool] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(bool(ok))
        print(f"\n{'✅' if ok else '❌'} {name}")
        if detail:
            print("   " + detail)

    def skip(name: str, why: str) -> None:
        print(f"\n⏭️  {name}")
        print("   " + why)

    print("=" * 78)
    print("Step 6 冒烟 · 前端托管 + API 契约（TestClient，不联网）")
    print("=" * 78)

    # ⓪ 先看鉴权当前是什么形态 —— 后面的断言范围由它决定，所以这一步必须在最前。
    h = client.get("/api/health")
    try:
        hj = h.json()
    except Exception as e:  # noqa: BLE001
        hj = {}
        print(f"   /api/health 不是合法 JSON（{type(e).__name__}: {e}）")
    auth_state = hj.get("auth") or {}
    auth_enabled = bool(auth_state.get("enabled"))
    print(f"\n鉴权形态：enabled={auth_enabled} jwt_secret_configured="
          f"{auth_state.get('jwt_secret_configured')} users_count={auth_state.get('users_count')} "
          f"seed_admin_present={auth_state.get('seed_admin_present')}")

    token: str | None = None
    token_src = "不需要（免鉴权形态）"
    if auth_enabled:
        token, token_src = _login(client, args.token)
        if token:
            client.headers["Authorization"] = f"Bearer {token}"   # 之后所有请求自动带票
        else:
            print(f"⚠️  {token_src}")
            print("⚠️  跳过鉴权端点，仅跑公开端点 —— 本次结果不能作为业务端点的通过凭据，"
                  "退出码将非 0。")
    print(f"令牌   ：{token_src}")

    # ① 前端页面：GET / → 200 + text/html（公开端点）
    r = client.get("/")
    ctype = r.headers.get("content-type", "")
    check("① GET / 返回 200 且 content-type 含 text/html",
          r.status_code == 200 and "text/html" in ctype,
          f"status={r.status_code} content-type={ctype!r}  响应体摘录：{_snip(r.text)}")

    # ② 健康检查：/api/health → frontend.available 为 True（公开端点）
    front = (hj.get("frontend") or {})
    check("② /api/health 的 frontend.available 为 True",
          h.status_code == 200 and front.get("available") is True,
          f"status={h.status_code} frontend={json.dumps(front, ensure_ascii=False)}  "
          f"响应体摘录：{_snip(h.text)}")

    # ③ 对比端点：GET /api/compare → ok=true 且 rows 长度 2（**需要票**）
    if token or not auth_enabled:
        c = client.get("/api/compare",
                       params={"indicator": "营业总收入", "codes": "600519,000858"})
        try:
            cj = c.json()
        except Exception as e:  # noqa: BLE001
            cj = {}
            print(f"   /api/compare 不是合法 JSON（{type(e).__name__}: {e}）")
        rows = cj.get("rows") or []
        check("③ /api/compare 返回 ok=true 且 rows 长度 2",
              c.status_code == 200 and cj.get("ok") is True and len(rows) == 2,
              f"status={c.status_code} ok={cj.get('ok')} rows={len(rows)}  "
              f"响应体摘录：{_snip(c.text)}")
    else:
        skip("③ /api/compare（需要 token）", "未取得令牌")

    # ④ 流式问答：POST /api/ask/stream 首个事件为 meta（**需要票**）
    #    注意是 POST，body 用 AskRequest 的字段；use_llm=False 不联网不花 token。
    if token or not auth_enabled:
        s = client.post("/api/ask/stream", json={
            "question": "贵州茅台2024年年报的审计机构是哪家",
            "use_llm": False,
        })
        first = first_sse_event(s.text)
        check("④ POST /api/ask/stream 首个事件为 meta",
              s.status_code == 200 and first == "meta",
              f"status={s.status_code} 首个事件={first!r}  响应体摘录：{_snip(s.text)}")
    else:
        skip("④ POST /api/ask/stream（需要 token）", "未取得令牌")

    # ⑤ 鉴权闸门：**无票**打业务端点必须 401（只在鉴权开着时有意义）
    if auth_enabled and token:
        # 显式把 Authorization 置空：TestClient 的 headers 会与显式头合并，
        # 空串在 Starlette 的 HTTPBearer(auto_error=False) 下等价于"没带头"。
        g = client.get("/api/compare", params={"indicator": "营业总收入", "codes": "600519"},
                       headers={"Authorization": ""})
        check("⑤ 无 token 访问 /api/compare → 401 且带 WWW-Authenticate",
              g.status_code == 401 and "www-authenticate" in {k.lower() for k in g.headers},
              f"status={g.status_code} headers={dict(g.headers)}  响应体摘录：{_snip(g.text)}")

    print("\n" + "=" * 78)
    ok = all(results)
    if ok and not auth_enabled:
        print("✅ Step 6 冒烟通过（免鉴权形态：业务端点的鉴权路径本次未被覆盖，"
              "契约见 tests/test_server_auth.py）")
        return 0
    if ok and auth_enabled and token:
        print("✅ Step 6 冒烟通过（鉴权形态：带票业务端点 + 无票闸门均已验证）")
        return 0
    if not results or all(results):
        print("⚠️ Step 6 冒烟未完成：公开端点全过，但鉴权端点因缺少凭据被跳过（见上方 ⏭️）")
    else:
        print("❌ Step 6 冒烟有失败项")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())