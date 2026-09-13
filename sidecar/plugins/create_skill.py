#!/usr/bin/env python3
"""create_skill —— 用自然语言把流程固化成技能或工具插件（NL plugin-creator）。

模型用法：用户说"把这套流程做成技能"时调用本工具，给出 kind/name/description
与 instructions（技能）或 code（插件）。生成物经静态校验 + 结构沙箱检查后
打包为 .latiaoext，走既有安装链装入（本地来源、权限分级、sha256 记录）。

安全边界（务必保持）：
- 不往 sidecar/plugins/ 直接写文件（启动即 import = 任意代码执行）；
- 不 import/执行生成物，只做 AST 静态分析与 zip 结构检查；
- 插件代码需声明不低于推断值的权限（readonly < files < network < shell）。
"""
import tempfile
from pathlib import Path

NAME = "create_skill"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "把当前对话里的做法固化为可复用的能力：kind='skill' 生成技能（模型今后通过 "
            "use_skill 调用），kind='plugin' 生成带代码的工具插件。"
            "生成前会做静态校验与结构沙箱检查，通过后自动安装。"
            "skill 需提供 instructions（触发场景 + 执行步骤，≥20 字）；"
            "plugin 需提供 code（Python，必须定义 NAME/DEFINITION/PERMISSION/execute）"
            "与 permission（readonly/files/network/shell，不得低于代码实际能力）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["skill", "plugin"],
                         "description": "产物类型：skill=技能说明；plugin=带代码的工具"},
                "name": {"type": "string", "description": "名称（字母/数字/-/_，2-41 位）"},
                "description": {"type": "string", "description": "一句话用途说明"},
                "instructions": {"type": "string",
                                 "description": "skill：何时使用 + 具体步骤（≥20 字）"},
                "code": {"type": "string",
                         "description": "plugin：完整 Python 源码（含 DEFINITION/execute）"},
                "permission": {"type": "string", "enum": ["readonly", "files", "network", "shell"],
                               "description": "plugin 权限声明（不得低于代码实际能力）"},
            },
            "required": ["kind", "name", "description"],
        },
    },
}


def execute(args: dict) -> str:
    kind = str(args.get("kind", "")).strip().lower()
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    if kind not in ("skill", "plugin"):
        return "Error: kind 必须是 skill 或 plugin"
    if not name:
        return "Error: name 必填"

    import plugin_creator as pc
    try:
        if kind == "skill":
            zip_bytes, meta = pc.build_skill_package(
                name, description, str(args.get("instructions", "")))
        else:
            code = str(args.get("code", ""))
            if not code.strip():
                return "Error: plugin 需要提供 code"
            zip_bytes, meta = pc.build_plugin_package(
                name, description, code, str(args.get("permission", "readonly")),
                str(args.get("instructions", "")))
    except ValueError as e:
        return f"⛔ 生成被拒绝：{e}"
    except Exception as e:
        return f"Error: 打包失败：{type(e).__name__}: {e}"

    check = pc.sandbox_check(zip_bytes)
    if not check["ok"]:
        return "⛔ 结构沙箱检查未通过：" + "；".join(check["errors"])

    from extension_manager import install_extension
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / f"{meta['name']}.latiaoext"
        p.write_bytes(zip_bytes)
        result = install_extension(str(p), "", label=f"local:{kind}")

    if result.get("status") != "ok":
        return f"⛔ 安装失败：{result.get('message', '未知错误')}"

    # 热加载：新技能/插件立即可用（与扩展页安装同一路径）
    reload_note = ""
    try:
        import asyncio
        from main import _hot_reload_extensions
        r = _hot_reload_extensions()          # 异步函数（扩展安装走同一路径）
        if asyncio.iscoroutine(r):
            try:
                r = asyncio.run(r)            # 工具在线程里执行：新起循环跑完
            except RuntimeError:
                # 已在事件循环内（不可重入）→ 兜底为提示，不影响安装结果
                r = None
        if isinstance(r, dict):
            reload_note = f"（已热加载：工具 {r.get('tools', '?')} 个 / 技能 {r.get('skills', '?')} 个）"
        else:
            reload_note = "（安装已完成；工具/技能将在下次请求时生效）"
    except Exception:
        reload_note = "（安装已完成；工具/技能将在下次请求时生效）"

    if kind == "skill":
        return (f"✅ 技能已创建：{meta['name']} v1.0.0{reload_note}\n"
                f"今后遇到相应场景，模型可调用 use_skill(\"{meta['name']}\") 加载这套流程。")
    return (f"✅ 插件已创建：{meta['name']} v1.0.0（权限：{meta['permissions'][0]}）{reload_note}\n"
            f"其工具已注册，按权限档位执行（confirm 档会先请你确认）。")
