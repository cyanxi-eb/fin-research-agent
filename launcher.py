"""一键启动器：预检 → 拉起服务 → 等 /api/health → 打开浏览器。

用法：
    python launcher.py                      # 本机起 uvicorn（默认 127.0.0.1:8000）
    python launcher.py --mode hybrid         # 启用向量 + RRF 混合检索
    python launcher.py --check               # 只做启动前预检，不起服务
    python launcher.py --no-auth             # 单机免登录形态（不校验令牌、不用配密钥）
    python launcher.py --no-browser --port 8123
    python launcher.py --docker              # 改走 docker compose up -d（app + MySQL）

设计约定（基线为 workflow-agent/launcher.py，按本项目实际结构调整）：
- 只负责「拉起」，不内嵌依赖：运行仍依赖项目内 .venv（Linux/macOS 走 .venv/bin/python）。
- 双击两次不会起两个服务：先探 /api/health，已在跑就直接开页面，**不重复起**。
- 预检失败给**可操作**的下一步（如「先跑 scripts/ingest_all.py」），而不是干巴巴的「文件不存在」。
- 退出时回收子进程：Windows 用 `CREATE_NEW_PROCESS_GROUP` 起子进程，Ctrl+C 只送到父进程，
  由父进程显式 terminate，避免留下占住端口的孤儿 uvicorn。

鉴权就绪（2026-09-23 补）：
- `FA_AUTH_ENABLED` 默认 **1**（安全默认），而 `FA_JWT_SECRET` 为空时服务**启动即失败**
  （刻意的：用默认密钥签名等于没鉴权）。但"一条命令启动"这条主入口不能因此变成必然崩溃，
  于是本机形态在密钥缺失时**现生成**一份写进 `data/db_keys.local.json`（已 gitignore，
  与 `scripts/deploy_vm.py` 给 VM 侧生成 `.env` 同一做法）—— 鉴权不裸奔，也不用用户手抄环境变量。
- 演示账号口令同样**现生成**：源码/文档里绝不写死口令，只在"本机新生成"时打印一次，
  让人能登进去（否则页面卡在登录页，谁也进不来）。已存在账号或已配置口令时不动、不打印。
- `--no-auth` 走单机免登录形态；`--check` 是**只读**的，不会写任何文件。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 仍在项目根，故直接把根加进 sys.path：`from src import config` 才能跑（读配置项而非硬编码路径）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"   # Windows
if not VENV_PY.exists():
    VENV_PY = ROOT / ".venv" / "bin" / "python"        # Linux/macOS

READY_TIMEOUT = 60          # 本机 uvicorn 就绪等待（秒）：要加载索引 + 图，留足余量
DOCKER_READY_TIMEOUT = 180  # 容器首次启动要建库 + 灌 seed，给足宽限期


# ============================ 基础工具 ============================

def _url(host: str, port: int) -> str:
    """拼对外访问地址；监听 0.0.0.0/:: 时本机访问要用回环地址。"""
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{shown}:{port}/"


def _probe_health(host: str, port: int, timeout: float = 2.0) -> dict | None:
    """探 GET /api/health：服务活着返回解析后的 JSON，否则 None（不抛异常）。"""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{probe_host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status == 200:
                return json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return None


def wait_ready(host: str, port: int, timeout: float = READY_TIMEOUT) -> bool:
    """轮询 /api/health 直到就绪或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe_health(host, port, timeout=2.0) is not None:
            return True
        time.sleep(0.8)
    return False


def _terminate(proc: subprocess.Popen) -> None:
    """确保子进程被回收：先 terminate（Windows 即 TerminateProcess），超时再 kill。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _print_problems(problems: list[str]) -> None:
    print(f"[预检未通过] 发现 {len(problems)} 个问题：")
    for i, p in enumerate(problems, 1):
        print(f"  {i}) {p}")
    print("修好后重跑本命令；只想看预检不动服务加 --check。")


# ============================ 鉴权就绪 ============================

def _env_file_has(name: str) -> bool:
    """`.env` 里有没有给这个键一个**非空**值（compose 拿它做变量默认）。"""
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name and v.strip().strip('"').strip("'"):
                return True
    except OSError:
        return False
    return False


def _write_db_keys(updates: dict) -> None:
    """把若干键**合并**写进 `data/db_keys.local.json`（该文件已 gitignore）。

    读-改-写而不是覆盖：里面还有 `mysql_*` / `backend` 等既有设置，
    覆盖掉等于把用户的库连接弄丢。
    """
    path = config.DB_KEYS_LOCAL_PATH
    data: dict = {}
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:                      # noqa: BLE001
        data = {}                          # 文件损坏按空处理（与 config._db_local 同一容错口径）
    data.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)              # Windows 上基本 no-op，Linux/macOS 上真正收权限
    except OSError:
        pass


def _seed_admin_exists() -> bool:
    """演示账号是否已在库里。查不到（库/表还没建）一律按"不存在"处理 —— 让口令能被打印出来。"""
    try:
        from src import db
        return db.get_user((config.SEED_ADMIN_USER or "").strip() or "admin") is not None
    except Exception:                      # noqa: BLE001
        return False


def _credential_hint(*, generated_pwd: str | None = None, will_generate: bool = False) -> list[str]:
    """给调用方打印的**登录凭据提示**。

    铁律：**只回显我们自己刚生成的口令**。别人配的口令（环境变量 / 本地文件）一个字都不回显，
    只说清它在哪 —— 否则日志、终端滚动记录都成了口令的第二份副本。
    """
    user = (config.SEED_ADMIN_USER or "").strip() or "admin"
    if generated_pwd:
        return [f"演示账号（本次现生成，只打印这一次）：{user} / {generated_pwd}",
                "↑ 已写进 data/db_keys.local.json（已 gitignore）；换台机器会另生成一份 —— "
                "源码与文档里没有默认口令。"]
    configured = (config.SEED_ADMIN_PASSWORD or "").strip()
    if configured:
        src = ("环境变量 SEED_ADMIN_PASSWORD" if os.getenv("SEED_ADMIN_PASSWORD", "").strip()
               else "data/db_keys.local.json 的 seed_admin_password")
        tail = "已存在" if _seed_admin_exists() else "会在启动时建出"
        return [f"演示账号 {user} {tail}；口令不在此回显（来源：{src}）"]
    if will_generate:
        return [f"演示账号 {user}：启动时会连口令一起现生成并打印一次"
                "（--check 是只读的，不生成）"]
    return [f"未配置演示账号口令：账号 {user} 不会被创建，登录页将没有可用账号。",
            "    → 设 SEED_ADMIN_PASSWORD（≥8 位）后重启，"
            "或加 --no-auth 走单机免登录形态"]


def ensure_local_auth(*, no_auth: bool, write: bool) -> tuple[bool, str, list[str]]:
    """本机形态的鉴权就绪处理。返回 `(ok, 说明, 要打印的凭据提示行)`。

    为什么需要这一步：`FA_AUTH_ENABLED` 默认 1，`FA_JWT_SECRET` 为空时**服务启动即失败**。
    那是**刻意的安全默认**（容器里忘了配就该起不来），但同一条规则搬到"一条命令本机启动"
    就变成了主入口必然崩溃 —— 用户只会看到一屏 Traceback。

    所以：密钥缺失时**现生成**写进 gitignore 的本地密钥文件，鉴权照旧开着、也不必手抄环境变量。
    口令同理：缺了就现生成，让人能登进去（否则页面卡在登录页、谁也进不来）；
    但**只在"是我们刚生成的"时回显一次**，源码与文档里永远没有默认口令。
    """
    if no_auth or not config.AUTH_ENABLED:
        return True, ("已关闭（单机免登录形态）：业务端点不校验令牌，"
                      "前端显示「单机模式（免登录）」"), []
    if (config.JWT_SECRET or "").strip():
        return True, "已启用（密钥来自环境变量或 data/db_keys.local.json）", _credential_hint()
    if not write:
        return (True,
                "已启用；本机缺密钥 → 启动时会自动生成一份写入 "
                "data/db_keys.local.json（已 gitignore）",
                _credential_hint(will_generate=not (config.SEED_ADMIN_PASSWORD or "").strip()))

    secret = secrets.token_urlsafe(48)
    updates = {"jwt_secret": secret}
    pwd = (config.SEED_ADMIN_PASSWORD or "").strip()
    generated_pwd = None
    if not pwd:
        pwd = secrets.token_urlsafe(12)
        updates["seed_admin_password"] = pwd
        generated_pwd = pwd
    _write_db_keys(updates)
    config.JWT_SECRET = secret             # 本进程同步：预检与日志都按"已就绪"读

    note = ("已启用；密钥缺失 → 本次已生成并写入 data/db_keys.local.json"
            "（已 gitignore，每台机器一份；源码里没有默认密钥）")
    # 账号已存在时 ensure_seed_admin 是幂等的、口令不会变，这时打印一个"新"口令是错的。
    show = generated_pwd if not _seed_admin_exists() else None
    return True, note, _credential_hint(generated_pwd=show)


# ============================ 预检 ============================

def _mysql_reachable(timeout: int = 3) -> tuple[bool, str]:
    """MySQL 是否可连（连服务 + 校验凭据 + 库是否存在）。返回 (ok, 说明)。"""
    m = config.MYSQL
    target = f"{m['user']}@{m['host']}:{m['port']}/{m['database']}"
    try:
        import pymysql  # 与运行时同一条通道（requirements 里带 [rsa]）
    except ImportError:
        # 退化为端口探测：只证明「有人在监听」，不校验凭据
        try:
            with socket.create_connection((m["host"], m["port"]), timeout=timeout):
                return True, f"{m['host']}:{m['port']}（仅端口探测，未装 pymysql）"
        except OSError as e:
            return False, f"{target}（{type(e).__name__}: {e}）"
    try:
        conn = pymysql.connect(
            host=m["host"], port=m["port"], user=m["user"], password=m["password"],
            database=m["database"], connect_timeout=timeout, read_timeout=timeout)
        conn.close()
        return True, target
    except Exception as e:  # 连接被拒 / 认证失败 / 未知库，都是"连不上"
        return False, f"{target}（{type(e).__name__}: {e}）"


def run_precheck(docker: bool, no_auth: bool = False) -> list[str]:
    """返回问题清单（空 = 通过）。每条都带**可操作**的下一步。"""
    problems: list[str] = []

    if docker:
        if shutil.which("docker") is None:
            problems.append(
                "未找到 docker 命令（本机没装 Docker 或不在 PATH 上）。\n"
                "    → 装 Docker Desktop 后重试；或去掉 --docker 用本机 .venv 直接起："
                "python launcher.py")
            return problems
        # 容器形态没法替用户改 .env（那是部署配置，不是本机临时状态），所以这里只报不修。
        # 少了这一条，`--check` 会说"通过"而容器一启动就死 —— 那正是 2026-09-23 本机踩到的坑。
        if (not no_auth and not os.getenv("FA_JWT_SECRET", "").strip()
                and not _env_file_has("FA_JWT_SECRET")):
            problems.append(
                "容器形态开启了鉴权（FA_AUTH_ENABLED 默认 1）但没有 FA_JWT_SECRET："
                "容器会启动即失败。\n"
                "    → ① 在 .env 里给 FA_JWT_SECRET 一个随机长串"
                "（python -c \"import secrets;print(secrets.token_urlsafe(48))\"），"
                "并一并配好 SEED_ADMIN_USER/SEED_ADMIN_PASSWORD —— 否则起了也登不进去；"
                "或 ② 免登录形态：python launcher.py --docker --no-auth")
        return problems

    if not VENV_PY.exists():
        problems.append(
            f"未找到虚拟环境解释器：{VENV_PY}\n"
            "    → 建环境并装依赖：python -m venv .venv，然后 "
            ".venv/Scripts/python.exe -m pip install -r requirements.txt")

    if not config.BM25_INDEX_PATH.exists():
        problems.append(
            f"缺 BM25 索引：{config.BM25_INDEX_PATH}\n"
            "    → 先建索引（取原文 → 解析 → 切分 → 建索引）："
            ".venv/Scripts/python.exe scripts/ingest_all.py")

    if config.DB_BACKEND == "mysql":
        ok, info = _mysql_reachable()
        if not ok:
            problems.append(
                f"MySQL 后端连不上：{info}\n"
                "    → ① 起库并核对 data/db_keys.local.json 的 mysql_*（或环境变量 MYSQL_*），"
                "再跑 .venv/Scripts/python.exe scripts/init_db.py；"
                "或 ② 换回 sqlite：把 data/db_keys.local.json 的 backend 改为 sqlite"
                "（或设 FA_DB_BACKEND=sqlite）后重跑")
    elif not config.DB_PATH.exists():
        problems.append(
            f"SQLite 业务库不存在：{config.DB_PATH}\n"
            "    → 先建表并灌数据：.venv/Scripts/python.exe scripts/init_db.py --fetch"
            "（--fetch 联网拉东财三大报表；只想建空表去掉 --fetch）")
    return problems


# ============================ 启动路径 ============================

def _report_check(args: argparse.Namespace, problems: list[str]) -> int:
    """--check：只报预检结果，退出码 = 预检是否通过（不再起服务、不碰端口状态）。"""
    print("=" * 60)
    print(f"启动前预检（模式：{'docker compose' if args.docker else '本机 uvicorn'}）")
    print("=" * 60)
    if problems:
        _print_problems(problems)
        return 1

    if args.docker:
        print("✓ docker CLI 可用")
    else:
        print(f"✓ 虚拟环境解释器：{VENV_PY}")
        print(f"✓ BM25 索引：{config.BM25_INDEX_PATH}")
        if config.DB_BACKEND == "mysql":
            ok, info = _mysql_reachable()
            print(f"✓ MySQL 后端可连：{info}" if ok else f"✗ MySQL：{info}")
        else:
            print(f"✓ SQLite 业务库：{config.DB_PATH}")

    # 鉴权就绪：**只读**（--check 不改任何文件）。这条在 2026-09-23 之前是缺的 ——
    # 预检说"通过"、紧接着启动就因缺 JWT_SECRET 崩掉。
    auth_ok, auth_note, _ = ensure_local_auth(no_auth=args.no_auth, write=False)
    print(f"{'✓' if auth_ok else '✗'} 鉴权：{auth_note}")

    if args.mode == "hybrid":
        manifest = config.VECTOR_DIR / config.VECTOR_MANIFEST_NAME
        if not manifest.exists():
            print(f"⚠ 检索模式 hybrid，但缺向量库 {manifest} —— 服务会静默降级为纯 BM25。\n"
                  "    → 建向量库：.venv/Scripts/python.exe scripts/index_vector.py")

    # 顺带报一下端口上的服务状态：真正启动时若已在运行就直接复用，不会重复起
    running = _probe_health(args.host, args.port, timeout=1.0) is not None
    print(f"✓ 端口 {args.port}："
          + ("已有服务在运行（启动时直接复用，不重复起）" if running else "空闲"))
    # 建议命令要能**原样复现**本次预检的形态：漏了 --no-auth 会让人以为要配密钥。
    hint = ("python launcher.py"
            + (" --docker" if args.docker else f" --port {args.port} --mode {args.mode}")
            + (" --no-auth" if args.no_auth else ""))
    print(f"预检通过。启动：{hint}")
    return 0


def run_local(args: argparse.Namespace) -> int:
    problems = run_precheck(docker=False, no_auth=args.no_auth)
    if args.check:
        return _report_check(args, problems)
    if problems:
        _print_problems(problems)
        return 1

    url = _url(args.host, args.port)
    # 已在跑就直接开页面：双击两次不会起两个服务
    if _probe_health(args.host, args.port, timeout=2.0) is not None:
        print(f"[提示] 服务已在运行，直接打开 {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    # 鉴权就绪：缺密钥就现生成一份写进 gitignore 的本地密钥文件（本机形态才自愈；
    # 容器形态由 .env 决定，预检会报出来）。放在"确认要启动"之后，避免白写文件。
    _, auth_note, hints = ensure_local_auth(no_auth=args.no_auth, write=True)
    print(f"[鉴权] {auth_note}")
    for line in hints:
        print(f"[鉴权] {line}")

    cmd = [str(VENV_PY), "-m", "uvicorn", "src.server:app",
           "--host", args.host, "--port", str(args.port)]
    env = os.environ.copy()
    env["RETRIEVE_MODE"] = args.mode
    if args.no_auth:
        env["FA_AUTH_ENABLED"] = "0"       # 子进程显式关鉴权（前端据此显示"单机模式"）
    kwargs: dict = {"cwd": str(ROOT), "env": env}
    if os.name == "nt":
        # 新进程组：Ctrl+C 只送到本进程，由本进程显式回收子进程（避免孤儿占端口）
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    print(f"[启动] {' '.join(cmd)}  (RETRIEVE_MODE={args.mode})")
    proc = subprocess.Popen(cmd, **kwargs)

    ready = False
    try:
        ready = wait_ready(args.host, args.port, timeout=READY_TIMEOUT)
        if ready:
            print(f"[就绪] {url}"
                  "（本窗口保持开着 = 服务运行中；Ctrl+C 停止服务）")
            if not args.no_browser:
                webbrowser.open(url)
        else:
            print(f"[警告] 服务 {READY_TIMEOUT} 秒内未就绪，请查看上方 uvicorn 日志排错")
        proc.wait()
    except KeyboardInterrupt:
        print("\n[停止] 收到 Ctrl+C，正在回收服务进程…")
    finally:
        _terminate(proc)
    if ready:
        print("[停止] 服务已关闭")
    return 0 if ready else 1


def run_docker(args: argparse.Namespace) -> int:
    problems = run_precheck(docker=True, no_auth=args.no_auth)
    if args.check:
        return _report_check(args, problems)
    if problems:
        _print_problems(problems)
        return 1

    env = os.environ.copy()
    env["RETRIEVE_MODE"] = args.mode
    if args.no_auth:
        env["FA_AUTH_ENABLED"] = "0"       # compose 用 ${FA_AUTH_ENABLED:-1}，这里覆盖它
    print(f"[鉴权] " + ("已关闭（--no-auth）：容器以单机免登录形态运行"
                       if args.no_auth else "按 .env / 环境变量（见 docker-compose.yml 的 FA_* 键）"))
    cmd = ["docker", "compose", "up", "-d"]
    print(f"[启动] {' '.join(cmd)}  (cwd={ROOT}, RETRIEVE_MODE={args.mode})")
    rc = subprocess.run(cmd, cwd=str(ROOT), env=env).returncode
    if rc != 0:
        print(f"[错误] docker compose up 退出码 {rc}，请查看上方输出排错")
        return rc

    url = _url(args.host, args.port)
    print(f"[等待] 轮询 {url}api/health（最多 {DOCKER_READY_TIMEOUT}s，"
          "首次启动要建库 + 灌 seed）…")
    if wait_ready(args.host, args.port, timeout=DOCKER_READY_TIMEOUT):
        print(f"[就绪] {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    print(f"[警告] {DOCKER_READY_TIMEOUT}s 内未就绪，排查："
          "docker compose logs --tail=120 app")
    return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="金融财报分析 Agent 一键启动器")
    ap.add_argument("--port", type=int, default=8000, help="服务端口（默认 8000）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--mode", choices=sorted(config.RETRIEVE_MODES),
                    default=config.RETRIEVE_MODE,
                    help="检索模式（默认取 config.RETRIEVE_MODE）")
    ap.add_argument("--no-browser", action="store_true", help="就绪后不自动打开浏览器")
    ap.add_argument("--no-auth", action="store_true",
                    help="单机免登录形态（等价 FA_AUTH_ENABLED=0）：业务端点不校验令牌，不用配密钥")
    ap.add_argument("--check", action="store_true", help="只做启动前预检，不起服务")
    ap.add_argument("--docker", action="store_true",
                    help="改走 docker compose up -d（app + MySQL），并等 /api/health")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_docker(args) if args.docker else run_local(args)


if __name__ == "__main__":
    raise SystemExit(main())