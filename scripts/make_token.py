"""换一个可用的访问令牌 —— 冒烟脚本与运维手工验证的统一入口。

```
python scripts/make_token.py                       # 用户名取配置默认，口令交互式输入
python scripts/make_token.py --username admin --ttl 30
python scripts/make_token.py --refresh             # 同时打印刷新令牌
```

## 为什么不走 HTTP 的 `POST /api/auth/login`

因为**要能在服务没起来时用**：冒烟脚本与被调试的服务常常死在"起不来"这一步，
而排查往往正需要一张票去打别的端点。走进程内调用（`src.auth.authenticate` →
`create_access_token`）还有个好处：它和 HTTP 端点**共用同一套校验**，
所以这里能签出票就等于登录逻辑本身是通的，不存在"两条路各写一遍、悄悄跑偏"。

## 口令怎么给（三种方式，按安全程度排序）

1. 环境变量 `FA_SMOKE_PASSWORD`（脚本化调用走这条，不落 shell history）；
2. 不传 `--password` 时**交互式输入**（`getpass`，不回显）—— 手工验证推荐这条；
3. `--password` 命令行入参：**会进 shell history 与进程列表**，只图省事时用。

用户名同理：`--username` > `FA_SMOKE_USER` > `config.SEED_ADMIN_USER`（默认 admin）。
口令若既不传也没配，则回落到 `config.SEED_ADMIN_PASSWORD`（= `SEED_ADMIN_PASSWORD`
环境变量或 `data/db_keys.local.json` 的 `seed_admin_password`），再没有就交互式问。

退出码：`0` 成功；`1` 认证失败（用户名/口令不对或账号被禁用）；`2` 环境不对（未配 JWT_SECRET）。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import auth, config, db  # noqa: E402


def _expiry_local(token: str) -> str:
    """从**签发出来的票本身**读出过期时间并转本地时区。

    刻意不"自己算一遍 now+ttl"：那样只能证明算术对，证明不了票里写的是什么。
    反解令牌才是在看真实产物（顺带证明这张票当场是能验过的）。
    """
    payload = auth.decode_token(token)
    exp = datetime.fromtimestamp(payload["exp"]).astimezone()
    return exp.strftime("%Y-%m-%d %H:%M:%S %Z")


def main() -> int:
    ap = argparse.ArgumentParser(description="换一个访问令牌（进程内登录，不需要服务在跑）")
    ap.add_argument("--username", default=os.getenv("FA_SMOKE_USER") or config.SEED_ADMIN_USER,
                    help=f"用户名（默认 {config.SEED_ADMIN_USER}；可用 FA_SMOKE_USER 覆盖）")
    ap.add_argument("--password",
                    default=os.getenv("FA_SMOKE_PASSWORD") or config.SEED_ADMIN_PASSWORD,
                    help="口令（默认取 FA_SMOKE_PASSWORD / SEED_ADMIN_PASSWORD；"
                         "留空则交互式输入）")
    ap.add_argument("--ttl", type=int, default=config.JWT_ACCESS_TTL_MIN,
                    help=f"访问令牌有效期（分钟，默认 {config.JWT_ACCESS_TTL_MIN}）")
    ap.add_argument("--refresh", action="store_true",
                    help=f"同时打印刷新令牌（默认 {config.JWT_REFRESH_TTL_DAYS} 天）")
    args = ap.parse_args()

    # users 表可能还没建（全新克隆后第一次跑本脚本）。建表是幂等的，顺手做掉，
    # 免得报一个 "no such table: users" 让人以为是鉴权坏了。
    db.init_schema()

    username = (args.username or "").strip()
    password = args.password or ""
    if not password:
        password = getpass.getpass(f"口令（{username}）：")
    if not password:
        print("❌ 没有口令：请用 --password、FA_SMOKE_PASSWORD 或交互式输入")
        return 1

    user = auth.authenticate(username, password)
    if user is None:
        # 刻意不区分"用户不存在 / 口令错 / 已禁用"：把差异说出来等于送一个用户名枚举接口。
        print(f"❌ 认证失败：用户名或口令不正确，或账号已禁用（{username}）")
        return 1

    # --ttl 直接改 config：auth 是在**签发的瞬间**读这个值的（见 auth._encode），
    # 所以这里改完立刻生效，不需要重启也不需要改文件。
    if args.ttl != config.JWT_ACCESS_TTL_MIN:
        config.JWT_ACCESS_TTL_MIN = args.ttl

    try:
        token = auth.create_access_token(user["sub"], user["role"], username=user["username"])
        refresh = auth.create_refresh_token(user["sub"], username=user["username"]) if args.refresh else None
    except RuntimeError as e:                      # 未配 JWT_SECRET（见 auth.require_jwt_secret）
        print(f"❌ 无法签发：{e}")
        return 2

    print(f"身份   : {user['username']}（sub={user['sub']}，role={user['role']}）")
    print(f"访问票 : {token}")
    print(f"  过期 : {_expiry_local(token)}（{args.ttl} 分钟）")
    if refresh:
        print(f"刷新票 : {refresh}")
        print(f"  过期 : {_expiry_local(refresh)}（{config.JWT_REFRESH_TTL_DAYS} 天）")
    print()
    print("用法（PowerShell，把 <TOKEN> 换成上面的访问票）：")
    print('  curl.exe -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/api/auth/me')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())