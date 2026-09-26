#!/bin/sh
# 容器启动脚本：准备数据卷 → 等数据库 → 建表 → 灌 seed → 起服务。
# 为什么把 init 放到启动时（而不是 build 时）：MySQL 后端下 build 阶段还没有数据库，
# 而且数据卷是运行期才挂上的。每一步都打印 [entrypoint] 前缀，便于 docker logs 定位。
set -e

echo "[entrypoint] 启动：FA_DB_BACKEND=${FA_DB_BACKEND:-sqlite}"

# ---- 1) 把镜像里烤进去的语料种到数据卷 ----
# compose 把卷挂在 /app/data，会遮住镜像里 /app/data 的内容，所以语料烤在 /app/seed。
# 这里首次启动复制过去：cp -rn 的 -n = 不覆盖已存在文件（用户往卷里放了真语料就以卷为准）。
if [ -d /app/seed/data ]; then
  cp -rn /app/seed/data/. /app/data/
  echo "[entrypoint] 已同步 seed 语料 → /app/data（cp -rn，不覆盖既有文件）"
else
  echo "[entrypoint] 未发现 /app/seed/data，跳过语料同步"
fi

# ---- 2) MySQL 后端：等数据库端口就绪（socket 探测，最多 60s）----
# 超时**必须明确报错并退出 1**：无声继续只会让后面的 init_db 报一串连接失败，
# 把「数据库根本没起来」伪装成「建表脚本有问题」。
if [ "${FA_DB_BACKEND:-}" = "mysql" ]; then
  echo "[entrypoint] 等待 MySQL ${MYSQL_HOST:-127.0.0.1}:${MYSQL_PORT:-3306} 就绪（最多 60s）…"
  python - <<'PY' || { echo "[entrypoint] 错误：等待 MySQL 超过 60s 仍未就绪，退出 1" >&2; exit 1; }
import os
import socket
import sys
import time

host = os.getenv("MYSQL_HOST", "127.0.0.1")
port = int(os.getenv("MYSQL_PORT", "3306"))
deadline = time.time() + 60
last = ""
while True:
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"[entrypoint] MySQL {host}:{port} 已就绪")
            sys.exit(0)
    except OSError as e:                      # 连接被拒 / 超时 / 名字解析失败
        last = f"{type(e).__name__}: {e}"
    if time.time() >= deadline:
        print(f"[entrypoint] MySQL {host}:{port} 等待超时（最后一次：{last}）", file=sys.stderr)
        sys.exit(1)
    time.sleep(2)
PY
fi

# ---- 3) 建表 + 同步公司/年报清单（幂等）----
echo "[entrypoint] 建表 + 同步清单：python scripts/init_db.py"
python scripts/init_db.py

# ---- 4) 灌 seed 业务数据（幂等：全部按主键 upsert，容器每次重启都跑也安全）----
echo "[entrypoint] 载入 seed 业务数据：python scripts/load_seed.py"
python scripts/load_seed.py

# ---- 5) 起服务（exec 让 uvicorn 接管 PID 1，能收到停止信号）----
echo "[entrypoint] 启动服务：$@"
exec "$@"