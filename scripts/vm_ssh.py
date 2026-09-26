# -*- coding: utf-8 -*-
"""用密码 SSH 登录远程机执行命令 / 上传文件（本机无 sshpass，改用 paramiko）。

用法：
    python scripts/vm_ssh.py                            # 跑内置巡检清单
    python scripts/vm_ssh.py "命令1" "命令2"            # 跑自定义命令
    python scripts/vm_ssh.py --sudo "命令"              # sudo 执行（自动喂密码）
    python scripts/vm_ssh.py --upload 本地目录 远程目录  # 递归上传（跳过噪声目录）
    python scripts/vm_ssh.py --upload . /home/vcvvcv/fin-research-agent --with-seed  # 连 seed/ 一起传

凭据**只从环境变量读**：VM_HOST / VM_USER / VM_PASSWORD / VM_PORT。
`VM_PASSWORD` 无默认值 —— 本脚本**不硬编码任何口令**；未设置时直接报错退出。
"""
from __future__ import annotations

import os
import posixpath
import sys
from pathlib import Path

import paramiko

HOST = os.getenv("VM_HOST", "192.168.57.128")
USER = os.getenv("VM_USER", "vcvvcv")
# ⚠️ 口令只允许来自环境变量（勿硬编码真实口令进仓库）。
PASSWORD = os.getenv("VM_PASSWORD", "")
PORT = int(os.getenv("VM_PORT", "22"))
TIMEOUT = 15

# 上传时跳过的目录／文件（本项目相关）
SKIP_DIRS = {".venv", ".venv.broken-cp314", "__pycache__", ".pytest_cache",
             "build", "dist", "node_modules", ".git"}
# seed/ 是**可选上传**：默认跳过（体积大，多数巡检用不到）；
# 需要时加 `--with-seed`（或设 VM_UPLOAD_SEED=1）。容器部署验收**必须**带上它。
OPTIONAL_SKIP_DIRS = {"seed"}
# db_keys.local.json / llm_keys.local.json 不上传：里面写的是本机 MySQL(127.0.0.1) 与
# 本机 Key，传到 VM 会造成误导；容器里一律用 compose 的 FA_DB_BACKEND / MYSQL_* / *_API_KEY
# 环境变量（优先级高于这些本地文件）。.env 同理（可能含真实口令/Key）。
SKIP_FILES = {".smoke_state.json", "db_keys.local.json", "llm_keys.local.json", ".env"}

# 内置巡检清单
RECON = [
    ("主机信息", "hostname; uname -r; (cat /etc/os-release 2>/dev/null | head -2)"),
    ("当前用户/组", "id"),
    ("docker 版本", "docker --version 2>&1 || echo 'docker 不可用'"),
    ("docker 服务状态", "systemctl is-active docker 2>&1"),
    ("磁盘", "df -h / | tail -1"),
    ("内存", "free -h 2>/dev/null | head -2"),
    ("PyPI 可达性", "curl -s -o /dev/null -w '%{http_code} %{time_total}s' --max-time 8 https://pypi.org/simple/ 2>&1 || echo FAIL"),
    ("已配 pip 镜像", "cat ~/.pip/pip.conf 2>/dev/null || cat /etc/pip.conf 2>/dev/null || echo '(无)'"),
]


def connect() -> paramiko.SSHClient:
    if not PASSWORD:
        raise SystemExit(
            "❌ 未设置 VM_PASSWORD 环境变量。本脚本不硬编码口令，请先：\n"
            "     $env:VM_PASSWORD='<远端 SSH 口令>'   # 然后重跑\n"
            f"   （目标 {USER}@{HOST}:{PORT}）")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(HOST, port=PORT, username=USER, password=PASSWORD,
                       timeout=TIMEOUT, allow_agent=False, look_for_keys=False)
    except Exception as e:
        raise SystemExit(f"❌ SSH 连接 {USER}@{HOST}:{PORT} 失败：{type(e).__name__}: {e}")
    return client


def run(client: paramiko.SSHClient, cmd: str, use_sudo: bool = False,
        timeout: float | None = 180):
    """执行命令。sudo 用 -S 从 stdin 读密码——注意不要用 get_pty，
    否则终端会把密码回显进 stdout（踩过）。

    注意：**不要在 cmd 里用 `&` 后台化**——后台进程会持有 SSH 通道的 fd，
    导致 recv_exit_status() 永远不返回（踩过）。长任务请用前台跑，
    由调用方在工具层做后台化（timeout=None 表示不设读超时）。
    """
    if use_sudo:
        cmd = f"sudo -S -p '' bash -c {cmd!r}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=False)
    if use_sudo:
        stdin.write(PASSWORD + "\n")
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    if PASSWORD:                      # 空口令时 replace("", ...) 会把整串打散，故加判空
        out = out.replace(PASSWORD, "***")
        err = err.replace(PASSWORD, "***")
    code = stdout.channel.recv_exit_status()
    # sudo -S 会把密码提示写到 stderr（-p '' 已置空，保险起见再滤一次）
    err = "\n".join(l for l in err.splitlines() if l.strip())
    return code, out.strip(), err.strip()


def upload_path(client: paramiko.SSHClient, local: Path, remote: str,
                *, with_seed: bool = False) -> tuple[int, int]:
    """上传单个文件或整个目录，返回 (文件数, 字节数)。

    `with_seed=False`（默认）时跳过 `seed/`；容器部署验收需要它，传 `with_seed=True`。
    """
    if local.is_file():
        sftp = client.open_sftp()
        try:
            sftp.put(str(local), remote)
        finally:
            sftp.close()
        return 1, local.stat().st_size

    skip = set(SKIP_DIRS) | (set() if with_seed else set(OPTIONAL_SKIP_DIRS))
    sftp = client.open_sftp()

    def ensure(remote_dir: str) -> None:
        parts, cur = remote_dir.strip("/").split("/"), ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except IOError:
                sftp.mkdir(cur)

    n = total = 0
    ensure(remote)
    for root, dirs, files in os.walk(local):
        dirs[:] = [d for d in dirs if d not in skip]
        rel = Path(root).relative_to(local)
        rdir = posixpath.join(remote, rel.as_posix()) if rel.as_posix() != "." else remote
        ensure(rdir)
        for f in files:
            if f in SKIP_FILES or f.endswith(".pyc"):
                continue
            lp = Path(root) / f
            rp = posixpath.join(rdir, f)
            try:
                sftp.put(str(lp), rp)
                n += 1
                total += lp.stat().st_size
            except Exception as e:
                print(f"  [skip] {lp.name}: {type(e).__name__}: {e}")
    sftp.close()
    return n, total


def main() -> None:
    args = sys.argv[1:]
    use_sudo = "--sudo" in args
    args = [a for a in args if a != "--sudo"]
    with_seed = "--with-seed" in args or os.getenv("VM_UPLOAD_SEED", "").strip() == "1"
    args = [a for a in args if a != "--with-seed"]
    # --timeout N：N=0 表示不设读超时（长构建用）
    timeout: float | None = 180
    if "--timeout" in args:
        i = args.index("--timeout")
        timeout = None if args[i + 1] == "0" else float(args[i + 1])
        del args[i:i + 2]

    client = connect()
    print(f"✅ SSH 已连接 {USER}@{HOST}:{PORT}\n")

    if args and args[0] == "--upload":
        if len(args) < 3:
            raise SystemExit("用法：--upload <本地文件或目录> <远程路径> [--with-seed]")
        local, remote = Path(args[1]).resolve(), args[2]
        n, total = upload_path(client, local, remote, with_seed=with_seed)
        print(f"✅ 已上传 {n} 个文件，共 {total / 1024:.1f} KB → {remote}"
              f"（seed/ {'已包含' if with_seed else '已跳过'}）")
        if local.is_file():
            code, out, err = run(client, f"ls -la {remote}")
            print(out)
        client.close()
        return

    if args:
        for c in args:
            code, out, err = run(client, c, use_sudo, timeout)
            print(f"$ {c}\n{out}")
            if err:
                print(f"  [stderr] {err}")
            print(f"  [exit={code}]\n")
        client.close()
        return

    for title, c in RECON:
        code, out, err = run(client, c)
        print("=" * 64)
        print(f"## {title}")
        print("=" * 64)
        print(out or "(无输出)")
        if err:
            print(f"[stderr] {err}")
        print()
    client.close()


if __name__ == "__main__":
    main()