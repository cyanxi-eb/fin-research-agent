"""工具注册表 —— 函数 + JSON Schema + 描述，统一导出给 LLM function calling。

为什么要注册表，而不是直接把函数塞进一个 dict：
1. **Schema 与实现同文件**。工具一旦被 LLM 调用，参数名/类型/枚举就是接口契约；
   把 Schema 写在别处（或让 LLM 自己猜），改名时必然漏改，表现为"模型调用总失败"
   却查不出原因。放一起，改函数签名时 Schema 就在同一屏。
2. **一个出口供三种宿主复用**：本地 function calling（OpenAI 兼容）、后续 MCP server、
   以及评估脚本直接调 `dispatch()`。三处若各自维护一份工具清单，迟早不一致。
3. **参数校验与错误兜底收在一处**。LLM 传参不可信（漏字段、多字段、类型错、
   编造一个不存在的工具名）。工具层**不能抛异常**——一抛整条链路就断，
   要返回结构化的 `{"ok": false, "error": ...}` 让模型能自我纠正。

约定：
- 每个工具的入参/出参都是**纯 JSON 可序列化**的 dict（方便审计落库与前端直出）。
- 工具**只读**：本层不写库、不联网（取数属 ingest 层职责）。这让工具可以放心地被
  LLM 反复调用而无需二次确认。
"""
from __future__ import annotations

from typing import Any, Callable

# name -> {name, description, parameters, function, returns}
_TOOLS: dict[str, dict] = {}

# 对外只读别名：其他模块（文档生成、审计、测试）要能遍历工具清单，
# 但不应自己造第二份注册表。用同一个 dict 对象，不拷贝。
TOOLS = _TOOLS


def tool(name: str, description: str, parameters: dict,
         returns: str | None = None) -> Callable:
    """装饰器：把一个纯函数注册成 LLM 可调用工具。

    `parameters` 必须是合法 JSON Schema（`type: object` + `properties` + `required`）。
    `description` 要写成**给模型看的说明书**：说明它能做什么、什么时候该用、
    以及有哪些"看起来能用其实不行"的边界（模型的误用大多源于描述里没写边界）。

    关于重复注册：`python -m src.tools.indicators` 这类调用会让同一文件被加载两次
    （一次作为 `src.tools.indicators`、一次作为 `__main__`），装饰器于是执行两遍。
    因此这里只在**不同工具撞名**时报错；同一源文件同一函数的重入视为刷新，直接覆盖。
    判据用 `__code__.co_filename`（不受加载路径影响的真实磁盘路径）而不是 `__module__`
    （后者在 -m 下会变成 `__main__`，正好不同，会误判成撞名）。
    """

    def deco(fn: Callable) -> Callable:
        src_file = getattr(getattr(fn, "__code__", None), "co_filename", "")
        new_sig = (src_file, fn.__qualname__)
        old = _TOOLS.get(name)
        if old is not None:
            old_fn = old["function"]
            old_sig = (getattr(getattr(old_fn, "__code__", None), "co_filename", ""),
                       getattr(old_fn, "__qualname__", ""))
            if old_sig != new_sig:
                raise ValueError(
                    f"工具名重复注册：{name}（工具名是 LLM 侧的唯一契约，不能撞）\n"
                    f"  已在册：{old_sig[0]}::{old_sig[1]}\n"
                    f"  本次注册：{new_sig[0]}::{new_sig[1]}")
        _TOOLS[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "function": fn,
            "returns": returns or "",
        }
        return fn

    return deco


def tool_names() -> list[str]:
    return sorted(_TOOLS)


def get_tool(name: str) -> dict | None:
    return _TOOLS.get(name)


def openai_tools() -> list[dict]:
    """导出 function calling 用的 tools 数组（OpenAI / DeepSeek / Qwen 兼容格式）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in _TOOLS.values()
    ]


def dispatch(name: str, arguments: dict | None = None, **kwargs) -> dict:
    """按名字调用工具，**永不抛异常**。

    返回里始终带 `ok`；失败时带 `error` 与人类可读的 `message`，
    好让模型知道该怎么改参数重试，而不是整条链路崩掉。

    支持的调用形态：
    - `dispatch("get_financial_indicator", {"company": "贵州茅台", "indicator": "营业总收入"})`
    - `dispatch("get_financial_indicator", company="贵州茅台", indicator="营业总收入")`
    """
    spec = _TOOLS.get(name)
    if spec is None:
        return {"ok": False, "error": "unknown_tool", "tool": name,
                "message": f"没有名为 {name!r} 的工具。可用工具：{tool_names()}",
                "available_tools": tool_names()}

    params = dict(arguments or {})
    params.update(kwargs)          # 显式 kwargs 优先于 arguments 里的同名项

    schema = spec["parameters"]
    required = schema.get("required") or []
    missed = [k for k in required if params.get(k) in (None, "")]
    if missed:
        return {"ok": False, "error": "missing_argument", "tool": name,
                "message": f"缺少必填参数 {missed}",
                "required": required, "schema": schema}

    allowed = set((schema.get("properties") or {}))
    unknown = [k for k in params if k not in allowed]
    if unknown:
        # 多传参数直接报错而不是忽略：静默忽略会让模型以为参数生效了，
        # 于是带着错误假设继续推理（例如以为 filter 生效、其实没生效）。
        return {"ok": False, "error": "unknown_argument", "tool": name,
                "message": f"不支持参数 {unknown}",
                "allowed": sorted(allowed), "schema": schema}

    try:
        result = spec["function"](**params)
    except Exception as exc:  # noqa: BLE001 —— 工具层兜底：任何异常都转成结构化错误
        return {"ok": False, "error": "tool_exception", "tool": name,
                "message": f"{type(exc).__name__}: {exc}"}

    if not isinstance(result, dict):
        return {"ok": False, "error": "bad_tool_return", "tool": name,
                "message": f"工具返回了非 dict：{type(result).__name__}"}
    result.setdefault("tool", name)
    return result


def describe() -> str:
    """给 CLI / 文档用的人读版工具清单。"""
    lines: list[str] = []
    for spec in _TOOLS.values():
        props = spec["parameters"].get("properties") or {}
        req = set(spec["parameters"].get("required") or [])
        args = ", ".join(
            f"{k}{'*' if k in req else ''}: {v.get('type', '?')}" for k, v in props.items())
        lines.append(f"- {spec['name']}({args})")
        lines.append(f"    {spec['description'].splitlines()[0]}")
    return "\n".join(lines)


# 注意：自检入口放在 `src/tools/__init__.py`（`python -m src.tools`），**不能**放这里。
# 原因：`python -m src.tools.registry` 会把本文件当 `__main__` **再加载一份**，
# 于是 `@tool` 注册进的是 `src.tools.registry._TOOLS`，而 `__main__._TOOLS` 是另一个空 dict
# —— 表现就是"明明注册了工具，却打印 0 个"。这类坑只在 -m 下出现，直接跑脚本不复现。
