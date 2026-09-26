# -*- coding: utf-8 -*-
"""Step 6 容器化 —— 在远端 VM 上做端到端部署与验收（同一套 paramiko 连接）。

```
# 完整流程：清空远端目录 → 上传（含 seed/）→ docker compose up -d --build → 断言
python scripts/deploy_vm.py

# 只对「已在运行」的远端栈做健康/接口断言（不构建、不上传）—— D8 的闸门用这个
python scripts/deploy_vm.py --verify-only

# 停掉远端 compose 栈（释放 VM 内存）
python scripts/deploy_vm.py --down

# 增量上传（不先清空远端项目目录，保留远端已有文件）
python scripts/deploy_vm.py --keep

# 把本机 data/llm_keys.local.json 的 Key 注入 VM 侧 .env（不吃 Key 时 vector 会降级）
python scripts/deploy_vm.py --keep --with-env-keys
```

CLI 开关语义（互不冲突）：
- `--verify-only`：跳过上传/构建，只对已在运行的服务做断言（要求上次是保留栈运行的）。
- `--down`：只执行 `docker compose down` 后退出。
- `--keep`：完整流程下**不先清空**远端项目目录（增量覆盖上传）。
- `--with-env-keys`：把本机 `data/llm_keys.local.json` 的非空 Key 一并写成 VM 侧 `<REMOTE_DIR>/.env`
  （权限 600，值不回显）。**默认关闭** —— 容器裸起时环境里没有 Key，
  向量通道会按设计降级为纯 BM25（`vector.available=false`）。

**v0.8.0 起：探针先登录换票，且新增两条断言。** 业务端点在安全默认（`FA_AUTH_ENABLED=1`）
下必须要 Bearer 票，故 compare / stream 探针先打 `/api/auth/login` 换票再带 `Authorization`；
断言里另加「无票访问业务端点必须 401 且带 `WWW-Authenticate`」与「容器内真实出网跑一次
provider 自检（D1 的 VM 侧实测）」。演示账号口令与 `FA_JWT_SECRET` 由本脚本**本机现生成**
写入 VM 侧 `.env`（值不回显）—— **不再挂在 `--with-env-keys` 后面**：不给密钥容器根本起不来
（`auth.require_jwt_secret` 的硬失败），所以它必须在每次完整部署时都写。
`--verify-only` 会从 VM 侧 `.env` 读回演示账号，同样拿得到票。

⚠️ 凭据只从环境变量读（VM_HOST / VM_USER / VM_PASSWORD / VM_PORT），脚本不硬编码口令；
   `--with-env-keys` 也不接受硬编码 Key，只读本机 `data/llm_keys.local.json`。

验收断言（与 `docs/plans/2026-09-22-step6-service-eval-container-workflow.md` D7 对齐）：
1. `/api/health` 的 `ok=true`，且 `index.ok` / `vector.available` / `regulation.available`
   均为真、`checkpointer.backend="mysql"` 且 `checkpointer.exists=true`；
2. `/api/health` 的 `auth.*` 三项（`enabled` / `jwt_secret_configured` / `seed_admin_present`）
   与 `web.enabled` / `web.provider_available` 均为真 —— **容器不该以免鉴权形态通过验收**；
3. 无票访问业务端点（`/api/compare`）必须 **401 且带 `WWW-Authenticate`**（本机测过 ≠ 容器里也拦得住）；
4. `/api/compare` 返回 `ok=true` 且 `rows` 长度 2（带票）；
5. `/api/ask/stream` 首个 SSE 事件为 `meta`（带票）；
6. 容器内真实出网跑一次 `python -m src.search.provider`（D1 的 VM 侧联网实测），退出码必须 0。
任一断言失败 → 自动 `docker compose logs --tail=120` 并**原样打印**（失败不缩成一句"没通过"）。

⚠️ 凭据只从环境变量读（VM_HOST / VM_USER / VM_PASSWORD / VM_PORT），脚本不硬编码口令。
⚠️ 宿主端口可用 `VM_APP_PORT` 覆盖（默认 8001）：VM 上 8000 已被姊妹项目占用；
   容器内始终监听 8000，故探针不受影响。
⚠️ HTTP 探针在**容器内**执行（`docker compose exec -T app python -c`），
   故 `http://127.0.0.1:8000/...` 指向 app 容器自身 —— 不依赖 VM 上是否有 curl、
   也不依赖本机能否直连 VM 的 8000 端口。
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import posixpath
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))   # 便于 `python scripts/deploy_vm.py`

from vm_ssh import HOST, PORT, USER, connect, run  # noqa: E402

REMOTE_DIR = f"/home/{USER}/fin-research-agent"

# 宿主侧端口映射。容器内**始终**监听 8000（Dockerfile EXPOSE 8000、CMD --port 8000、
# 镜像内 HEALTHCHECK 也打 127.0.0.1:8000），所以本变量只影响 `docker compose` 起栈时的
# **宿主端口**，不改任何容器内行为。
# 为什么需要它：VM 192.168.57.128 的宿主 8000 已被姊妹项目 workflow-agent 的容器占用
# （`0.0.0.0:8000->8000/tcp`），本项目若照默认 8000:8000 起会 `port is already allocated`。
# 故这里默认设 8001（对应 docker-compose.yml 的 `${APP_PORT:-8000}:8000`）。
# 本文件里的 HTTP 探针一律走 `docker compose exec` **进容器内**打 127.0.0.1:8000，
# 因此不受宿主端口影响（也就不需要跟着改成 8001）。
VM_APP_PORT = os.getenv("VM_APP_PORT", "8001")

# ---- 上传裁剪（与 .dockerignore 同思路，但这里按「项目根相对路径前缀」判断）----
# 只传「镜像/运行时必需」的东西；seed/ 必须传（Dockerfile 要 COPY 它），
# data/regulation 也必须传（Dockerfile 有 COPY）。
EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".ipynb_checkpoints", ".git",
                     ".venv", "venv", "build", "dist", "node_modules"}
EXCLUDE_REL_PREFIXES = ("data/raw", "data/parsed", "data/index", "data/vector", "data/db",
                        "eval", "docs", "tests")
EXCLUDE_FILE_GLOBS = ("*.pyc", "*.pyo", "*.local.json", "*.db", ".env")


def _rel_excluded(rel: str) -> bool:
    for p in EXCLUDE_REL_PREFIXES:
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


def _file_excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in EXCLUDE_FILE_GLOBS)


def upload_project(client, local_root: Path) -> tuple[int, int]:
    """递归上传项目到 REMOTE_DIR（按前缀裁剪大目录，但保留 seed/ 与 data/regulation）。"""
    sftp = client.open_sftp()

    def ensure(rdir: str) -> None:
        parts, cur = rdir.strip("/").split("/"), ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except IOError:
                sftp.mkdir(cur)

    ensure(REMOTE_DIR)
    n = total = 0
    for root, dirs, files in os.walk(local_root):
        rel = Path(root).relative_to(local_root).as_posix()
        rel = "" if rel == "." else rel
        if rel and _rel_excluded(rel):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES]
        rdir = posixpath.join(REMOTE_DIR, rel) if rel else REMOTE_DIR
        ensure(rdir)
        for f in files:
            if _file_excluded(f):
                continue
            lp, rp = Path(root) / f, posixpath.join(rdir, f)
            try:
                sftp.put(str(lp), rp)
                n += 1
                total += lp.stat().st_size
            except Exception as e:            # noqa: BLE001
                print(f"  [skip] {lp}: {type(e).__name__}: {e}")
    sftp.close()
    return n, total


# ---- 在 app 容器内执行的探针源码（base64 传递，避免多层 shell 引号地狱）----
#
# v0.8.0 起业务端点在 `FA_AUTH_ENABLED=1`（安全默认）下必须要 Bearer 票，故
# compare / stream 两个探针**先登录换票**。凭据经 `docker compose exec -e` 注入容器进程环境
# （`FA_PROBE_USER` / `FA_PROBE_PASSWORD`），**不写进探针源码** —— 也就不进 base64 命令行。
_HEALTH_SRC = """\
import urllib.request
b = urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5).read().decode("utf-8")
print(b)
"""

_LOGIN_HELPER = """\
import json, os, urllib.request

BASE = "http://127.0.0.1:8000"

def login():
    payload = json.dumps({"username": os.environ.get("FA_PROBE_USER", ""),
                          "password": os.environ.get("FA_PROBE_PASSWORD", "")}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/auth/login", data=payload,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))["access_token"]
"""

_COMPARE_SRC = _LOGIN_HELPER + """\
import urllib.parse
tok = login()
qs = urllib.parse.urlencode({"indicator": "营业总收入", "codes": "600519,000858"})
req = urllib.request.Request(BASE + "/api/compare?" + qs,
                             headers={"Authorization": "Bearer " + tok})
print(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
"""

_STREAM_SRC = _LOGIN_HELPER + """\
tok = login()
payload = json.dumps({"question": "贵州茅台2024年年报的审计机构是哪家", "use_llm": False}).encode("utf-8")
req = urllib.request.Request(BASE + "/api/ask/stream", data=payload,
                             headers={"Content-Type": "application/json",
                                      "Authorization": "Bearer " + tok})
text = urllib.request.urlopen(req, timeout=120).read().decode("utf-8", "replace")
first = next((l.split(":", 1)[1].strip() for l in text.split("\\n") if l.startswith("event:")), None)
print(json.dumps({"first_event": first, "head": text[:400]}, ensure_ascii=False))
"""

# 无票闸门：业务端点必须 401 且带 `WWW-Authenticate`。容器里也验一遍 ——
# 「本机测过 401」不等于「容器里也拦得住」（容器可能被别处的环境变量关掉了鉴权）。
_NOAUTH_SRC = """\
import json, urllib.error, urllib.parse, urllib.request
qs = urllib.parse.urlencode({"indicator": "营业总收入", "codes": "600519"})
try:
    urllib.request.urlopen("http://127.0.0.1:8000/api/compare?" + qs, timeout=30)
    print(json.dumps({"status": 200, "www_authenticate": None}))
except urllib.error.HTTPError as e:
    print(json.dumps({"status": e.code, "www_authenticate": e.headers.get("WWW-Authenticate")}))
"""

# D1 的 VM 侧联网实测：在容器里真实出网跑一次 provider 自检，退出码与原始输出原样带回。
_WEBSEARCH_SRC = """\
import json, subprocess, sys
r = subprocess.run([sys.executable, "-m", "src.search.provider", "贵州茅台 2024 年营业总收入"],
                   capture_output=True, text=True, cwd="/app", timeout=120)
print(json.dumps({"code": r.returncode, "out": (r.stdout or "")[:1200],
                  "err": (r.stderr or "")[:400]}, ensure_ascii=False))
"""


def vm_python(client, src: str, *, timeout: float | None = 60, env: dict | None = None):
    """在 app 容器内跑一段 python（用于 HTTP 探针）。返回 (code, out, err)。

    `env` 里的键值经 `docker compose exec -e` 注入容器进程环境；探针凭据走这条路，
    不进 base64 源码（命令行里也就看不到口令）。
    """
    b64 = base64.b64encode(src.encode("utf-8")).decode("ascii")
    env_flags = "".join(f" -e {k}={v}" for k, v in (env or {}).items())
    cmd = (f"cd {REMOTE_DIR} && docker compose exec -T{env_flags} -e PYTHONIOENCODING=utf-8 app "
           f"python -c \"import base64;exec(base64.b64decode('{b64}'))\"")
    return run(client, cmd, timeout=timeout)


def _parse_json(out: str):
    """从探针 stdout 里取 JSON（容忍 docker compose 的少量噪声行）。"""
    text = (out or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return None


def poll_health(client, budget: float = 180) -> dict:
    """轮询 /api/health 直到拿到可解析的 JSON，最多 budget 秒。"""
    deadline = time.time() + budget
    last = ""
    while True:
        code, out, err = vm_python(client, _HEALTH_SRC, timeout=30)
        if code == 0:
            body = _parse_json(out)
            if isinstance(body, dict):
                return body
            last = f"非 JSON 输出：{(out or '')[:200]}"
        else:
            last = f"exec 退出码 {code}（app 可能还没起来）err={err[:200]}"
        if time.time() >= deadline:
            raise AssertionError(f"{budget:.0f}s 内 /api/health 未就绪（最后一次：{last}）")
        time.sleep(5)


def assert_health(h: dict) -> list[str]:
    """按实际 `/api/health` 结构断言，返回未过项列表（空 = 全过）。

    注意：真实的 health JSON **没有顶层 backend 字段** —— 后端信息在
    `checkpointer.backend`；`index` 用 `ok`，`vector`/`regulation` 用 `available`，
    `checkpointer` 用 `exists` + `backend`。计划里"顶层 backend"的表述与本项目实际不符。
    """
    problems: list[str] = []
    if h.get("ok") is not True:
        problems.append(f"ok != true（实际 {h.get('ok')!r}）")

    idx = h.get("index") or {}
    if idx.get("ok") is not True:
        problems.append(f"index.ok != true（实际 {idx.get('ok')!r}，{idx.get('error') or idx.get('message') or ''}）")

    vec = h.get("vector") or {}
    if vec.get("available") is not True:
        problems.append(f"vector.available != true（实际 {vec.get('available')!r}，reason={vec.get('reason')!r}）")

    reg = h.get("regulation") or {}
    if reg.get("available") is not True:
        problems.append(f"regulation.available != true（实际 {reg.get('available')!r}）")

    ck = h.get("checkpointer") or {}
    if ck.get("backend") != "mysql":
        problems.append(f"checkpointer.backend != 'mysql'（实际 {ck.get('backend')!r}）")
    if ck.get("exists") is not True:
        problems.append(f"checkpointer.exists != true（实际 {ck.get('exists')!r}，error={ck.get('error')!r}）")

    # v0.8.0 新增：容器**不该以免鉴权形态验收** —— 若 auth.enabled=false，说明部署时
    # 有人把总开关关掉了，这份"验收通过"就名不副实（业务端点其实裸奔）。
    auth = h.get("auth") or {}
    if auth.get("enabled") is not True:
        problems.append(f"auth.enabled != true（实际 {auth.get('enabled')!r}）—— 容器不该以免鉴权形态验收")
    if auth.get("jwt_secret_configured") is not True:
        problems.append(f"auth.jwt_secret_configured != true（实际 {auth.get('jwt_secret_configured')!r}）")
    if auth.get("seed_admin_present") is not True:
        problems.append(f"auth.seed_admin_present != true（实际 {auth.get('seed_admin_present')!r}）")

    web = h.get("web") or {}
    if web.get("enabled") is not True:
        problems.append(f"web.enabled != true（实际 {web.get('enabled')!r}）")
    if web.get("provider_available") is not True:
        problems.append(f"web.provider_available != true（实际 {web.get('provider_available')!r}，"
                        f"backend={web.get('backend')!r}）")
    return problems


# ---- VM 侧 .env：鉴权密钥（本机现生成）+ 演示账号 + 联网开关，可选注入 LLM Key ----
#
# 为什么**必须有**这个函数（v0.8.0 起）：`FA_JWT_SECRET` 为空且 `FA_AUTH_ENABLED=1`
# 时应用**启动即失败**（刻意的安全默认，见 auth.require_jwt_secret）—— 也就是说
# 不给密钥的容器根本起不来，容器化验收必须带上真值。
# 为什么密钥/口令**本机现生成**而不是写死在仓库或 compose 默认值里：默认凭据等于没有鉴权。
# 安全约定：
#   ① 值只写进 VM 的 <REMOTE_DIR>/.env（该路径在本脚本的上传排除表里，也在本机 .gitignore 中）；
#   ② 日志只打印「键名」，**绝不回显值**；
#   ③ LLM Key 仍受 `--with-env-keys` 控制（默认关闭 → vector 按设计降级为纯 BM25）。
LOCAL_KEYS = ROOT / "data" / "llm_keys.local.json"
ENV_KEY_NAMES = {"qwen": "QWEN_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}
REMOTE_ENV = posixpath.join(REMOTE_DIR, ".env")


def push_runtime_env(client, *, with_llm_keys: bool) -> None:
    """写 VM 侧 `.env`：鉴权密钥 + 演示账号 + 联网开关（+ 可选 LLM Key）。

    compose 会自动读项目目录下的 `.env`，故写完再 `docker compose up -d` 即可生效。
    """
    lines = ["# 由 scripts/deploy_vm.py 生成；**不要提交进仓库**",
             "# ---- 鉴权（JWT）----",
             "FA_AUTH_ENABLED=1",
             f"FA_JWT_SECRET={secrets.token_urlsafe(48)}",
             "SEED_ADMIN_USER=admin",
             f"SEED_ADMIN_PASSWORD={secrets.token_urlsafe(12)}",
             "# ---- 联网搜索兜底 ----",
             "FA_WEB_SEARCH_ENABLED=1",
             "FA_WEB_SEARCH_BACKEND=bing"]
    names = ["FA_AUTH_ENABLED", "FA_JWT_SECRET", "SEED_ADMIN_USER", "SEED_ADMIN_PASSWORD",
             "FA_WEB_SEARCH_ENABLED", "FA_WEB_SEARCH_BACKEND"]
    if with_llm_keys:
        if not LOCAL_KEYS.exists():
            print(f"[deploy] ⚠ 未找到 {LOCAL_KEYS}，跳过 Key 注入（vector 通道会按设计降级）")
        else:
            data = json.loads(LOCAL_KEYS.read_text(encoding="utf-8"))
            present = [(src, env) for src, env in ENV_KEY_NAMES.items()
                       if str(data.get(src) or "").strip()]
            if not present:
                print("[deploy] ⚠ 本机 llm_keys.local.json 里没有非空 Key，跳过注入")
            lines += [f"{env}={str(data[src]).strip()}" for src, env in present]
            names += [env for _, env in present]
    sftp = client.open_sftp()
    with sftp.file(REMOTE_ENV, "w") as f:
        f.write("\n".join(lines) + "\n")
    sftp.chmod(REMOTE_ENV, 0o600)
    sftp.close()
    print(f"[deploy] 已写 VM 侧 {REMOTE_ENV}（权限 600）：{', '.join(names)} —— 值不回显")


def read_remote_credentials(client) -> dict:
    """从 VM 侧 `.env` 里读演示账号，供业务端点探针换票（`--verify-only` 时也拿得到）。

    值不回显；读不到口令时**明确告警**而不是静默让探针去撞 401。
    """
    code, out, _ = run(client, f"cat {REMOTE_ENV} 2>/dev/null || true")
    data: dict[str, str] = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    creds = {"user": data.get("SEED_ADMIN_USER") or "admin",
             "password": data.get("SEED_ADMIN_PASSWORD") or ""}
    if not creds["password"]:
        print(f"[deploy] ⚠ VM 侧 {REMOTE_ENV} 里没有 SEED_ADMIN_PASSWORD（文件缺失或未配）"
              f"→ 业务端点探针会因拿不到票而失败")
    else:
        print(f"[deploy] 已从 VM 侧 .env 读到演示账号 {creds['user']}（口令不回显）")
    return creds


def dump_logs(client) -> None:
    """失败现场：原样打印远端 `docker compose logs --tail=120`。"""
    print("\n[deploy] ======== 失败现场：docker compose logs --tail=120（原样） ========")
    code, out, err = run(client, f"cd {REMOTE_DIR} && docker compose logs --tail=120", timeout=180)
    print(out or "(无 stdout)")
    if err:
        print("[stderr] " + err)
    print(f"[deploy] ======== logs 结束（exit={code}） ========\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="在远端 VM 上部署并验收 Step 6 容器化（app + MySQL）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify-only", action="store_true",
                    help="跳过上传/构建，只对已在运行的服务做健康与接口断言")
    ap.add_argument("--down", action="store_true",
                    help="停掉远端 docker compose 栈后退出")
    ap.add_argument("--keep", action="store_true",
                    help="完整流程下不先清空远端项目目录（增量覆盖上传）")
    ap.add_argument("--with-env-keys", action="store_true",
                    help="把本机 data/llm_keys.local.json 的 Key 一并写进 VM 侧 .env（默认关闭）；"
                         "容器裸起时不给 Key，vector 通道会按设计降级为纯 BM25")
    args = ap.parse_args()

    client = connect()
    print(f"[deploy] SSH 已连接 {USER}@{HOST}:{PORT}；远端目录 {REMOTE_DIR}")
    try:
        if args.down:
            code, out, err = run(client, f"cd {REMOTE_DIR} && docker compose down", timeout=180)
            print(out or "(无 stdout)")
            if err:
                print("[stderr] " + err)
            print(f"[deploy] docker compose down exit={code}")
            return 0 if code == 0 else 1

        # ---- ① 上传 + 构建 + 起栈（--verify-only 跳过）----
        if not args.verify_only:
            if not args.keep:
                print("[deploy] 清空远端项目目录（保证干净重传；--keep 可跳过）…")
                run(client, f"rm -rf {REMOTE_DIR}", timeout=180)
            print("[deploy] 上传项目（跳过 .venv/data 等大目录；**包含 seed/** 与 data/regulation）…")
            n, total = upload_project(client, ROOT)
            print(f"[deploy] 已上传 {n} 个文件，共 {total / 1024 / 1024:.1f} MB")
            # 鉴权密钥与演示口令**本机现生成**后写入 VM .env（不给密钥容器起不来，见函数注释）
            push_runtime_env(client, with_llm_keys=args.with_env_keys)
            print("[deploy] docker compose up -d --build（长任务，读超时设 None）"
                  f"；宿主端口 APP_PORT={VM_APP_PORT}（容器内仍是 8000）…")
            code, out, err = run(client,
                                 f"cd {REMOTE_DIR} && APP_PORT={VM_APP_PORT} docker compose up -d --build",
                                 timeout=None)
            print((out or "")[-4000:])
            if err:
                print("[stderr] " + err[-4000:])
            if code != 0:
                print(f"[deploy] ❌ docker compose up 退出码 {code}")
                dump_logs(client)
                return 1

        failed = False

        # ---- ② /api/health（公开端点，不需要票）----
        try:
            health = poll_health(client)
        except AssertionError as e:
            print(f"[deploy] ❌ /api/health 轮询失败：{e}")
            dump_logs(client)
            return 1
        print("[deploy] /api/health 原文：\n" + json.dumps(health, ensure_ascii=False, indent=2))
        problems = assert_health(health)
        if problems:
            failed = True
            print("[deploy] ❌ /api/health 断言未过：")
            for p in problems:
                print("   - " + p)
        else:
            print("[deploy] ✅ /api/health：ok=true、index/vector/regulation 可用、"
                  "checkpointer.backend=mysql、auth 已启用、web 通道可用")

        # 业务端点探针要票：演示账号从 VM 侧 .env 读（--verify-only 也能读到）
        creds = read_remote_credentials(client)
        probe_env = {"FA_PROBE_USER": creds["user"], "FA_PROBE_PASSWORD": creds["password"]}

        # ---- ③ 无票闸门：业务端点必须 401 且带 WWW-Authenticate ----
        code, out, err = vm_python(client, _NOAUTH_SRC, timeout=60)
        nj = _parse_json(out)
        if code == 0 and (nj or {}).get("status") == 401 and (nj or {}).get("www_authenticate"):
            print(f"[deploy] ✅ 无票访问 /api/compare → 401（WWW-Authenticate="
                  f"{(nj or {}).get('www_authenticate')!r}）")
        else:
            failed = True
            print(f"[deploy] ❌ 无票闸门断言未过：exit={code} 得到 {(nj or {}).get('status')!r} "
                  f"WWW-Authenticate={(nj or {}).get('www_authenticate')!r}\n"
                  f"   原文：{(out or '')[:400]}\n   stderr：{err[:300]}")

        # ---- ④ /api/compare（带票）----
        code, out, err = vm_python(client, _COMPARE_SRC, timeout=60, env=probe_env)
        cj = _parse_json(out)
        rows = (cj or {}).get("rows") or []
        if code == 0 and isinstance(cj, dict) and cj.get("ok") is True and len(rows) == 2:
            print(f"[deploy] ✅ /api/compare 返回 ok=true 且 rows 长度 2（indicator={cj.get('indicator')!r}）")
        else:
            failed = True
            print(f"[deploy] ❌ /api/compare 断言未过：exit={code} ok={(cj or {}).get('ok')!r} "
                  f"rows={len(rows)}\n   原文：{(out or '')[:600]}\n   stderr：{err[:300]}")

        # ---- ⑤ /api/ask/stream 首个事件（带票）----
        code, out, err = vm_python(client, _STREAM_SRC, timeout=180, env=probe_env)
        sj = _parse_json(out)
        first = (sj or {}).get("first_event")
        if code == 0 and first == "meta":
            print(f"[deploy] ✅ /api/ask/stream 首个事件 = meta（head={(sj or {}).get('head', '')[:120]!r}）")
        else:
            failed = True
            print(f"[deploy] ❌ /api/ask/stream 首个事件 != meta：exit={code} first={first!r}\n"
                  f"   原文：{(out or '')[:600]}\n   stderr：{err[:300]}")

        # ---- ⑥ VM 侧联网实测（D1 的「VM 各跑一次」）：容器内真实出网跑 provider 自检 ----
        code, out, err = vm_python(client, _WEBSEARCH_SRC, timeout=180)
        wj = _parse_json(out)
        if code == 0 and (wj or {}).get("code") == 0 and "命中=" in ((wj or {}).get("out") or ""):
            hit = next((l for l in ((wj or {}).get("out") or "").splitlines() if "命中=" in l), "")
            print(f"[deploy] ✅ VM 侧联网实测：provider 自检退出码 0 —— {hit.strip()}")
        else:
            failed = True
            print(f"[deploy] ❌ VM 侧联网实测未过：exit={code} provider_exit={(wj or {}).get('code')!r}\n"
                  f"   原文：{((wj or {}).get('out') or (out or ''))[:600]}\n"
                  f"   stderr：{((wj or {}).get('err') or err or '')[:300]}")

        if failed:
            dump_logs(client)
            print("[deploy] ❌ 验收未通过（见上方断言与日志原文）")
            return 1
        print("[deploy] ✅ 容器化验收全部通过：health 六项（含 auth/web）+ 无票 401 闸门 + "
              "/api/compare 2 行 + /api/ask/stream 首帧 meta + VM 侧联网实测")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())