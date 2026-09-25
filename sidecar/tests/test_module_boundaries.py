"""模块边界守卫（2026-09-23 拆分 agent_loop.py 的第一批）。

拆分用的是"定义搬到新模块 + 旧位置 re-export"的迁移模式。这个模式有一个已知陷阱：
**如果有人哪天在旧位置又复制一份同名实现**，就会出现两份会各自漂移的代码——
正是审计点名的"静默漂移"（薄循环重构丢过功能、靠注释记回归）。

所以这里把"re-export 必须是同一个对象"钉成断言；再补一条"新模块不反向 import
枢纽做模块级依赖"，防止把回环重新引进来。
"""
import importlib
import inspect
from pathlib import Path

EXTRACTED = {
    "agent.progress": [
        "PROGRESS_FILE",       # 常量同样只能有一份定义（路径漂移是最隐蔽的一类）
        "_progress_file", "_record_progress", "_rotate_progress_file",
        "_progress_tail", "_clean_progress_tail",
    ],
    "agent.prompt_build": [
        "_build_chat_messages",
    ],
    "agent.tool_exec": [
        "execute_tool", "_handle_tool_execution", "_handle_tool_execution_inner",
        "_record_tool_call_db", "_stamp_time_sensitive", "_get_agent_tools",
    ],
    "agent.confirm": [
        "_start_tool_confirmation", "_await_tool_confirmation", "_confirm_bypassed",
        "_count_successful_duplicates",
    ],
}


def test_reexports_are_the_same_object():
    """agent_loop 的 re-export 必须与规范模块同一个对象（不是复制一份）。"""
    hub = importlib.import_module("agent_loop")
    for mod_name, names in EXTRACTED.items():
        mod = importlib.import_module(mod_name)
        for n in names:
            target = getattr(mod, n, None)
            assert target is not None, f"{mod_name}.{n} 不存在"
            assert getattr(hub, n, None) is target, (
                f"agent_loop.{n} 与 {mod_name}.{n} 不是同一对象——"
                "说明有人复制了第二份实现（会各自漂移），请改回 re-export")


def test_extracted_modules_have_no_module_level_hub_import():
    """新模块不得在模块级 import agent_loop（那会重新造出循环依赖）。

    允许函数内惰性导入（如 prompt_build 里取 AGENT_PROFILES），因为这个项目
    既有的约定就是靠惰性导入掰开回环。
    """
    for mod_name in EXTRACTED:
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(mod)
        for line in src.splitlines():
            if line.startswith(("import agent_loop", "from agent_loop import")):
                raise AssertionError(f"{mod_name} 在模块级 import 了 agent_loop：{line}")


def test_hub_still_exposes_the_old_api():
    """旧导入路径（api_routes/main/测试）必须继续可用。"""
    hub = importlib.import_module("agent_loop")
    for n in ("_progress_file", "_record_progress", "_progress_tail",
              "_clean_progress_tail", "_build_chat_messages", "execute_tool",
              "_handle_tool_execution", "_await_tool_confirmation"):
        assert callable(getattr(hub, n, None)), f"agent_loop.{n} 丢失（re-export 断了）"


def test_local_llm_reexports_are_the_same_object():
    """local_llm 门面 re-export 的 probe 符号必须同一对象（不是复制一份）。

    2026-09-24 local_llm 巨石拆分：patch local_llm_probe._config_file 后
    local_llm._config_file 必须跟着变，否则旧测试/工具会静默失效。
    """
    facade = importlib.import_module("local_llm")
    probe = importlib.import_module("local_llm_probe")
    for n in ("MODELS_DIR", "_read_config", "_config_file", "_gguf_precheck",
              "_mlx_arch_supported", "EngineProcess"):
        if n == "EngineProcess":
            target = getattr(importlib.import_module("local_llm_engine"), n)
        else:
            target = getattr(probe, n)
        assert getattr(facade, n, None) is target, (
            f"local_llm.{n} 与规范模块不是同一对象——re-export 断了或被复制")


# 2026-09-24 拆出的路由模块：不得模块级 import 枢纽（agent_loop / main）
ROUTE_MODULES = (
    "api_routes_admin",
    "api_routes_cron_local",
    "api_routes_extensions",
    "api_routes_media",
)


def test_route_modules_have_no_module_level_hub_import():
    """路由模块模块级 import 枢纽 = 循环依赖温床；必须函数内惰性导入。"""
    for mod_name in ROUTE_MODULES:
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(mod)
        for line in src.splitlines():
            if line.startswith(("import agent_loop", "from agent_loop import",
                                "import main", "from main import")):
                raise AssertionError(f"{mod_name} 在模块级 import 了枢纽：{line}")


def test_json_body_has_single_definition():
    """`_json_body` 五副本是漂移温床；规范定义只在 http_json.json_body。"""
    import http_json
    assert callable(http_json.json_body)
    assert http_json._json_body is http_json.json_body
    root = Path(http_json.__file__).resolve().parent
    defs = []
    for p in root.glob("api_routes*.py"):
        text = p.read_text(encoding="utf-8")
        if "async def _json_body" in text or "def _json_body" in text:
            defs.append(p.name)
    assert defs == [], f"路由模块仍自带 _json_body 副本：{defs}"
