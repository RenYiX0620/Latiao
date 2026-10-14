"""权限词表映射层的一致性测试（2026-10-11 审计修复）。

四套词表（UI 档位 / 工具危险级 / 插件声明 / 免确认集合）此前没有映射层，
这里把"翻译只有一份"钉住：镜像定义必须与各处权威定义一致，否则测试红。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "perm-token")


def test_vocabularies_are_consistent():
    """镜像一致、映射覆盖完整——任何一处改名而别处没跟上，这里直接红。"""
    import permissions_map as pm
    problems = pm.audit_vocabulary_consistency()
    assert problems == [], f"权限词表不一致: {problems}"


def test_plugin_perm_translation():
    import permissions_map as pm
    assert pm.plugin_perm_to_tool_level("readonly") == "safe"
    assert pm.plugin_perm_to_tool_level("files") == "confirm"
    assert pm.plugin_perm_to_tool_level("network") == "confirm"
    assert pm.plugin_perm_to_tool_level("shell") == "danger"
    # 未知值按最严处理，不静默降级
    assert pm.plugin_perm_to_tool_level("whatever") == "deny"
    assert pm.plugin_perm_to_tool_level("") == "deny"


def test_unknown_ui_mode_never_escalates():
    """审计 H2 的承诺：未知档位绝不静默升权（回落 confirm，不是 full）。"""
    from agent.context import _normalize_access
    assert _normalize_access("wtf") == "confirm"
    assert _normalize_access("workspace") == "auto_edit"   # 旧档位迁移
    assert _normalize_access("full") == "full"


def test_permissions_map_wired_into_extension_load():
    """permissions_map 必须被生产加载路径调用（不再是纯镜像层）。

    2026-10-11 深审：plugin_perm_to_tool_level 全仓无生产调用——manifest 的
    shell/network 声明翻译不到工具危险级，作者把 PERMISSION 自称 safe 即可免确认。
    """
    src = Path(__file__).resolve().parents[1].joinpath("tool_system.py").read_text("utf-8")
    assert "from permissions_map import plugin_perm_to_tool_level" in src
    assert "_ext_manifest_perms" in src
    # 取更严者，而不是无条件信任 plugin.py 的自称
    assert "manifest 声明更严" in src


def test_manifest_stricter_than_self_declared():
    """manifest=shell、自称 safe → 必须落到 danger（取更严）。"""
    from permissions_map import plugin_perm_to_tool_level
    _rank = {"safe": 0, "confirm": 1, "danger": 2, "deny": 3}
    self_declared = "safe"
    manifest_level = plugin_perm_to_tool_level("shell")
    assert _rank[manifest_level] > _rank[self_declared]
    # 未知 manifest 权限按 deny（最严），不能被 safe 盖过
    assert plugin_perm_to_tool_level("bogus") == "deny"
