"""导出工具清单与 function calling schema（自检 + 可核对产物）。

为什么要把 schema 落盘成 JSON：接大模型时最常见的一类故障是"模型调用工具总失败"，
而根因往往只是 schema 里少了个 `required`、或参数名和实现不一致。
落一份 `data/tools_schema.json`，就能：
1) 肉眼比对 schema 与函数签名是否一致；
2) 把它直接贴进 Postman / curl 复现模型看到的那份契约；
3) 作为"工具层接口冻结"的证据留档（改接口时 diff 看得见）。

用法：
    python scripts/list_tools.py            # 打印并写 data/tools_schema.json
    python scripts/list_tools.py --no-write # 只打印
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.tools import TOOLS, describe, openai_tools  # noqa: E402


def main() -> int:
    no_write = "--no-write" in sys.argv[1:]

    print(f"已注册 {len(TOOLS)} 个工具：\n{describe()}\n")

    payload = {"count": len(TOOLS), "tools": openai_tools()}
    print("=== function calling schema ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not no_write:
        out = config.DATA_DIR / "tools_schema.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
