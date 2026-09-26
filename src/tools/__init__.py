"""工具层 —— LLM 可调用的只读工具集合。

装配顺序有讲究：**先导入 registry，再导入各工具模块**。
各工具模块用 `@tool` 装饰器往 registry 里注册，所以只要模块被导入过，工具就在册；
反过来若 registry 反过来 import 工具模块，就会形成循环导入。

对外只需要三个东西：
- `openai_tools()`：丢给 LLM 的 tools 数组
- `dispatch(name, args)`：执行调用（永不抛异常）
- `TOOLS`：注册表本身，供文档/审计/测试使用
"""
from __future__ import annotations

from src.tools import registry  # noqa: F401  必须先于工具模块导入

# 导入即注册（顺序无所谓，工具名互不冲突）
from src.tools import indicators  # noqa: F401,E402
from src.tools import ratios  # noqa: F401,E402

from src.tools.registry import (  # noqa: E402
    TOOLS,
    describe,
    dispatch,
    get_tool,
    openai_tools,
    tool,
    tool_names,
)

__all__ = [
    "TOOLS", "describe", "dispatch", "get_tool", "openai_tools", "tool", "tool_names",
]
