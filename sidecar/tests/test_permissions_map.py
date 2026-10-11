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
