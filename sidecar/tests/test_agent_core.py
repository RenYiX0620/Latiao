"""Scope 容器单测（Cordis 式「万物皆插件」的核心语义）。"""
import asyncio

import pytest

from agent.core import Scope, ScopeError, Waterfall


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


def test_register_tool_fail_loud_on_conflict():
    s = Scope("t")

    def dispatch_a(a):
        return "x"

    s.register_tool(_tool("read_file"), dispatch=dispatch_a, permission="safe")
    with pytest.raises(ScopeError):
        s.register_tool(_tool("read_file"), dispatch=lambda a: "y")  # 不同 dispatch = 冲突
    # 同一函数对象的幂等注册允许
    s.register_tool(_tool("read_file"), dispatch=dispatch_a)


def test_restrict_tools_hides_from_catalog_and_dispatch():
    s = Scope("t")
    s.register_tool(_tool("read_file"), dispatch=lambda a: "r")
    s.register_tool(_tool("run_cmd"), dispatch=lambda a: "c")
    s.restrict_tools({"read_file"})
    names = {t["function"]["name"] for t in s.tools()}
    assert names == {"read_file"}
    assert s.dispatch_for("run_cmd") is None  # 执行被拒
    assert s.dispatch_for("read_file") is not None


def test_child_scope_inherits_and_can_narrow():
    s = Scope("root")
    s.register_tool(_tool("read_file"), dispatch=lambda a: "r")
    s.register_tool(_tool("run_cmd"), dispatch=lambda a: "c")
    child = s.child("sub")
    child.restrict_tools({"read_file"})
    assert {t["function"]["name"] for t in child.tools()} == {"read_file"}
    # 父不受影响
    assert {t["function"]["name"] for t in s.tools()} == {"read_file", "run_cmd"}


def test_sections_ordered_merge():
    s = Scope("t")
    s.register_section("b", "第二段", order=2)
    s.register_section("a", "第一段", order=1)
    assert s.sections() == "第一段\n\n第二段"


def test_waterfall_order_and_rewrite():
    async def add_one(payload, ctx):
        return payload + 1

    async def times_two(payload, ctx):
        return payload * 2

    w = Waterfall("request")
    w.register(times_two, order=2)
    w.register(add_one, order=1)
    result = asyncio.run(w.run(3))
    assert result == 8  # (3+1)*2 — order 决定链序


def test_waterfall_fail_loud():
    async def boom(payload, ctx):
        raise RuntimeError("hook exploded")

    w = Waterfall("pre_step")
    w.register(boom)
    with pytest.raises(RuntimeError):
        asyncio.run(w.run("x"))


def test_services_inherit_from_parent():
    root = Scope("root")
    root.provide("engine_url", "http://127.0.0.1:1235")
    child = root.child("sub")
    assert child.get_service("engine_url") == "http://127.0.0.1:1235"
    child.provide("engine_url", "override")
    assert child.get_service("engine_url") == "override"
    assert root.get_service("engine_url") == "http://127.0.0.1:1235"
    assert child.get_service("missing", "dft") == "dft"


def test_unknown_waterfall_fails_loud():
    s = Scope("t")
    with pytest.raises(ScopeError):
        s.waterfall("nope")
