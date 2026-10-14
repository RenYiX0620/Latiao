"""权限词表的唯一映射层（2026-10-11 审计修复：此前四套词表各自为政）。

**问题**：同一个"权限"在四处各有一套名字，没有任何映射层——插件作者、UI 用户、
工具实现各说各话，极易配错（审计原文："四套并行权限词汇表无映射层"）：
- ① UI/会话档位（用户在聊天框看到的五档）：read_only / confirm / auto_edit / plan / full
  （+ subagent 内部档）——权威定义与归一化在 `agent/context.py:ACCESS_LEVELS` /
  `_normalize_access`
- ② 工具危险级（每个工具写死的确认要求）：safe / confirm / danger / deny
  ——权威定义在 `capability_registry.py:_VALID_PERMS`（工具注册表读插件 manifest，
  缺省兜底见 `tool_executor._FALLBACK_PERMISSIONS`）
- ③ 插件能力声明（插件作者在 manifest 里写的）：readonly / files / network / shell
  ——权威定义在 `plugin_creator.py:_VALID_PERMS`
- ④ 免确认集合（运行时闸门用的白名单）：`agent/context.AUTO_EDIT_TOOLS` 等

**本模块只做三件事**：把四套词表与映射关系写成代码、集中在一处；给两侧提供
翻译函数（插件声明→危险级；危险级→用户在 ① 里需要放行到什么档）；并用一致性
测试钉住"四套定义没有漂移"。**不改变任何现有强制点**——各处的 enforcement 仍
读各自的原始定义（context.ACCESS_LEVELS 等），这里保证的是"翻译只有一份"。

用法：
- 插件系统翻译插件权限：`plugin_perm_to_tool_level("network")` → "confirm"
- 想知道某工具在哪些档位需要确认：`tool_level_to_ui_hint("confirm")` → 人话
- 一致性自检（测试）：`audit_vocabulary_consistency()`
"""
from __future__ import annotations

# ① UI/会话档位（权威在 agent/context.ACCESS_LEVELS；这里镜像并声明顺序语义）
UI_ACCESS_MODES: dict[str, str] = {
    "read_only": "只读：任何写操作都拒",
    "confirm": "默认档：写文件/命令等高危操作逐次确认",
    "auto_edit": "文件类工具免确认（write_file/open_folder），命令仍确认",
    "plan": "先出计划、确认后执行",
    "full": "完全访问：仅少量高危仍确认",
    "subagent": "子代理内部档：目录收窄 + 只读命令免确认",
}

# ② 工具危险级（权威在 capability_registry._VALID_PERMS）
TOOL_LEVELS: tuple[str, ...] = ("safe", "confirm", "danger", "deny")

# ③ 插件能力声明（权威在 plugin_creator._VALID_PERMS）
PLUGIN_PERMS: tuple[str, ...] = ("readonly", "files", "network", "shell")

# 插件声明 → 工具危险级（plugin_creator 的 inferred_permission 语义：
# readonly 不需要确认；files 写文件要确认；network/shell 高危）
PLUGIN_TO_TOOL_LEVEL: dict[str, str] = {
    "readonly": "safe",
    "files": "confirm",
    "network": "confirm",
    "shell": "danger",
}

# 工具危险级 → 用户在 ① 里至少要到哪一档才能"少被打扰"（说明性映射，供 UI 文案
# 与排查使用；不参与强制——强制仍是各处自己的表）
TOOL_LEVEL_UI_HINT: dict[str, str] = {
    "safe": "read_only 档即可（只读工具免确认）",
    "confirm": "confirm 档逐次确认；auto_edit 档对文件类工具免确认",
    "danger": "任何档位都要确认（full 档的少量高危例外）",
    "deny": "任何档位都拒绝",
}


def plugin_perm_to_tool_level(plugin_perm: str) -> str:
    """插件声明的权限 → 工具危险级。未知值按最严处理（不静默降级）。"""
    return PLUGIN_TO_TOOL_LEVEL.get(str(plugin_perm or "").strip().lower(), "deny")


def tool_level_to_ui_hint(tool_level: str) -> str:
    return TOOL_LEVEL_UI_HINT.get(str(tool_level or "").strip().lower(), "未知危险级")


def audit_vocabulary_consistency() -> list[str]:
    """四套词表的一致性自检：返回问题清单（空 = 一致）。

    检查点（都能在测试里钉住，防止将来某处改了名字而别处没跟上）：
    1. 本文档镜像的 ①/②/③ 与各自权威定义完全一致；
    2. 映射表覆盖全部插件权限；
    3. 续期档位（legacy workspace）仍能归一化到有效档位。
    """
    problems: list[str] = []
    try:
        from agent.context import ACCESS_LEVELS, _normalize_access
        if set(UI_ACCESS_MODES) != set(ACCESS_LEVELS):
            problems.append(
                f"① UI 档位镜像不一致：context.ACCESS_LEVELS={sorted(ACCESS_LEVELS)} "
                f"vs UI_ACCESS_MODES={sorted(UI_ACCESS_MODES)}")
        if _normalize_access("workspace") != "auto_edit":
            problems.append("旧档位 workspace 未归一化到 auto_edit")
        if _normalize_access("不存在的档位") != "confirm":
            problems.append("未知档位未回落到 confirm（审计 H2 要求绝不静默升权）")
    except Exception as e:  # pragma: no cover
        problems.append(f"读取 ① 定义失败: {e}")
    try:
        from capability_registry import _VALID_PERMS as caps
        if set(TOOL_LEVELS) != set(caps):
            problems.append(
                f"② 工具危险级镜像不一致：registry={sorted(caps)} vs {sorted(TOOL_LEVELS)}")
    except Exception as e:  # pragma: no cover
        problems.append(f"读取 ② 定义失败: {e}")
    try:
        from plugin_creator import _VALID_PERMS as plug
        if set(PLUGIN_PERMS) != set(plug):
            problems.append(
                f"③ 插件权限镜像不一致：plugin_creator={sorted(plug)} vs {sorted(PLUGIN_PERMS)}")
    except Exception as e:  # pragma: no cover
        problems.append(f"读取 ③ 定义失败: {e}")
    missing = [p for p in PLUGIN_PERMS if p not in PLUGIN_TO_TOOL_LEVEL]
    if missing:
        problems.append(f"映射表未覆盖插件权限: {missing}")
    unknown_targets = [v for v in PLUGIN_TO_TOOL_LEVEL.values() if v not in TOOL_LEVELS]
    if unknown_targets:
        problems.append(f"映射目标不是有效工具危险级: {unknown_targets}")
    return problems
