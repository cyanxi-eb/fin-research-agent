"""工具注册表测试（src/tools/registry.py）。

注册表是"LLM 看到的接口契约"，所以这里测的是**契约本身的自洽性**，而不是业务逻辑：
1. schema 里 `required` 的字段必须在 `properties` 里存在；
2. schema 里的每个参数名必须真的在函数签名里 —— 这条能挡住"改了函数签名忘了改 schema"
   这类漂移（表现是模型调用永远失败，且错误信息指向模型而不是代码）；
3. LLM 传参不可信：多传、漏传、传错类型、编造工具名，都必须返回结构化错误而**不抛异常**，
   否则一次误调用就能把整条链路打断。
"""
from __future__ import annotations

import inspect

import pytest

from src.tools import TOOLS, dispatch, get_tool, openai_tools, tool as tool_decorator


EXPECTED_TOOLS = {
    "get_financial_indicator",
    "compare_companies",
    "calc_financial_ratio",
    "list_supported",
}


def test_expected_tools_are_registered():
    assert EXPECTED_TOOLS <= set(TOOLS), f"缺工具：{EXPECTED_TOOLS - set(TOOLS)}"


def test_openai_tools_shape():
    specs = openai_tools()
    assert len(specs) == len(TOOLS)
    for spec in specs:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert set(fn) == {"name", "description", "parameters"}
        assert fn["parameters"]["type"] == "object"
        assert isinstance(fn["description"], str) and len(fn["description"]) > 20, \
            "描述太短：模型选工具/填参数全靠这段话，必须有实质内容"
        assert "properties" in fn["parameters"]


def test_schema_required_is_subset_of_properties():
    for name, spec in TOOLS.items():
        props = set(spec["parameters"].get("properties") or {})
        required = set(spec["parameters"].get("required") or [])
        assert required <= props, f"{name}: required 里有 properties 里不存在的字段 " \
                                 f"{required - props}"


def test_schema_properties_match_function_signature():
    """schema 与函数签名必须一致 —— 挡住"改了签名忘了改 schema"的漂移。"""
    for name, spec in TOOLS.items():
        sig = inspect.signature(spec["function"])
        params = set(sig.parameters)
        props = set(spec["parameters"].get("properties") or {})
        assert props == params, (
            f"{name}: schema 参数 {sorted(props)} 与函数签名 {sorted(params)} 不一致")
        # 必填项不能有默认值（有默认值就该是可选的）
        for req in spec["parameters"].get("required") or []:
            assert sig.parameters[req].default is inspect.Parameter.empty, \
                f"{name}.{req} 标了必填，但函数签名给了默认值"


def test_dispatch_happy_path_with_dict(synth_db):
    got = dispatch("get_financial_indicator",
                   {"company": "贵州茅台", "indicator": "营业总收入"})
    assert got["ok"] is True
    assert got["tool"] == "get_financial_indicator"
    assert got["series"][0]["value"] == pytest.approx(1.02e11)


def test_dispatch_happy_path_with_kwargs(synth_db):
    got = dispatch("calc_financial_ratio", company="贵州茅台", ratio="毛利率")
    assert got["ok"] is True and got["display"] == "60.00%"


def test_dispatch_unknown_tool_returns_available(synth_db):
    got = dispatch("make_me_coffee", {"a": 1})
    assert got["ok"] is False
    assert got["error"] == "unknown_tool"
    assert set(got["available_tools"]) == set(TOOLS)


def test_dispatch_missing_required_argument(synth_db):
    got = dispatch("get_financial_indicator", {"company": "贵州茅台"})
    assert got["ok"] is False
    assert got["error"] == "missing_argument"
    assert "indicator" in got["message"]
    assert got["required"] == ["company", "indicator"]


def test_dispatch_empty_string_counts_as_missing(synth_db):
    """空串按缺失处理：模型常把不填的参数序列化成 ""，这不该被当成合法取值。"""
    got = dispatch("get_financial_indicator", {"company": "", "indicator": "营业总收入"})
    assert got["ok"] is False and got["error"] == "missing_argument"


def test_dispatch_unknown_argument_is_error_not_silently_ignored(synth_db):
    """多传参数要报错而不是忽略：静默忽略会让模型以为参数生效了。"""
    got = dispatch("get_financial_indicator",
                   {"company": "贵州茅台", "indicator": "营业总收入", "unit": "亿元"})
    assert got["ok"] is False
    assert got["error"] == "unknown_argument"
    assert "unit" in got["message"]


def test_dispatch_swallows_tool_exception(synth_db, monkeypatch):
    """工具内部抛异常 → 转成结构化错误，不能让异常穿透打断链路。"""
    spec = dict(TOOLS["get_financial_indicator"])

    def boom(**kwargs):
        raise RuntimeError("模拟下游炸了")

    monkeypatch.setitem(TOOLS, "get_financial_indicator", {**spec, "function": boom})
    got = dispatch("get_financial_indicator",
                   {"company": "贵州茅台", "indicator": "营业总收入"})
    assert got["ok"] is False
    assert got["error"] == "tool_exception"
    assert "RuntimeError" in got["message"]


def test_dispatch_rejects_non_dict_tool_return(synth_db, monkeypatch):
    spec = dict(TOOLS["get_financial_indicator"])
    monkeypatch.setitem(TOOLS, "get_financial_indicator",
                        {**spec, "function": lambda **kwargs: "我是个字符串"})
    got = dispatch("get_financial_indicator",
                   {"company": "贵州茅台", "indicator": "营业总收入"})
    assert got["ok"] is False and got["error"] == "bad_tool_return"


def test_duplicate_tool_name_from_other_module_is_rejected():
    """不同来源的文件撞同一个工具名 → 必须显式报错（工具名是 LLM 侧唯一契约）。"""
    with pytest.raises(ValueError, match="工具名重复注册"):
        tool_decorator(name="get_financial_indicator", description="x" * 30,
                       parameters={"type": "object", "properties": {},
                                   "required": []},
                       )(lambda: None)


def test_get_tool_returns_none_for_unknown():
    assert get_tool("nope") is None
    assert get_tool("list_supported")["name"] == "list_supported"
