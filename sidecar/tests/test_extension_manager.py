"""扩展管理器回归测试：zip 安全 / manifest 校验 / 安装全链路 / 启用禁用。"""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import extension_manager as em


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


MANIFEST = """name: demo-ext
version: 1.0.0
description: 测试扩展
author:
  name: test
permissions:
  - network
"""

PLUGIN_PY = '''
NAME = "demo_tool"
PERMISSION = "safe"
DEFINITION = {"type": "function", "function": {"name": "demo_tool", "description": "demo", "parameters": {"type": "object", "properties": {}}}}
def execute(args):
    return "ok"
'''

SKILL_MD = """---
name: demo-skill
description: 测试技能
---
技能正文
"""


class TestExtensionManager(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        em.EXTENSIONS_DIR = Path(self._tmp.name) / "extensions"
        em.INSTALLED_FILE = em.EXTENSIONS_DIR / ".installed.json"
        self.addCleanup(self._tmp.cleanup)

    def _install(self, files):
        return em.install_extension("", "", "")  # placeholder

    def test_path_traversal_blocked(self):
        evil = _make_zip({"../evil.txt": "x"})
        with tempfile.TemporaryDirectory() as td:
            self.assertRaises(ValueError, em._safe_extract, evil, Path(td))
        abs_p = _make_zip({"/etc/evil": "x"})
        with tempfile.TemporaryDirectory() as td:
            self.assertRaises(ValueError, em._safe_extract, abs_p, Path(td))

    def test_zip_bomb_blocked(self):
        em._MAX_EXTRACT_BYTES = 1024
        bomb = _make_zip({"big.bin": "A" * 2048})
        with tempfile.TemporaryDirectory() as td:
            self.assertRaises(ValueError, em._safe_extract, bomb, Path(td))

    def test_manifest_required(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as td:
            zp = Path(td) / "x.zip"
            zp.write_bytes(_make_zip({"plugin.py": PLUGIN_PY}))  # 无 manifest
            res = em.install_extension(str(zp))
            self.assertEqual(res["status"], "error")
            self.assertIn("manifest", res["message"])

    def test_bad_name_version_rejected(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as td:
            zp = Path(td) / "x.zip"
            zp.write_bytes(_make_zip({
                "manifest.yaml": MANIFEST.replace("demo-ext", "BAD NAME!!"),
                "plugin.py": PLUGIN_PY,
            }))
            res = em.install_extension(str(zp))
            self.assertEqual(res["status"], "error")

    def test_full_install_flow(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as td:
            zp = Path(td) / "demo.zip"
            zp.write_bytes(_make_zip({
                "manifest.yaml": MANIFEST,
                "plugin.py": PLUGIN_PY,
                "skills/demo.md": SKILL_MD,
            }))
            res = em.install_extension(str(zp))
            self.assertEqual(res["status"], "ok", res)
            self.assertEqual(res["name"], "demo-ext")
            self.assertEqual(res["permissions"], ["network"])
            # 列出
            lst = em.list_extensions()
            self.assertEqual(len(lst), 1)
            self.assertTrue(lst[0]["enabled"])
            self.assertTrue(lst[0]["has_plugin"])
            self.assertTrue(lst[0]["has_skills"])
            # 目录落盘
            pkg = em.EXTENSIONS_DIR / "demo-ext" / "1.0.0"
            self.assertTrue((pkg / "plugin.py").exists())
            self.assertTrue((pkg / "skills" / "demo.md").exists())
            # 重复安装
            res2 = em.install_extension(str(zp))
            self.assertEqual(res2["status"], "error")
            # active dirs
            active = [d.name for d in em.active_extension_dirs()]
            self.assertIn("1.0.0", active)
            # 禁用后 active 消失
            em.set_extension_enabled("demo-ext", False)
            self.assertEqual(len(em.active_extension_dirs()), 0)
            em.set_extension_enabled("demo-ext", True)
            self.assertEqual(len(em.active_extension_dirs()), 1)
            # 卸载
            res3 = em.uninstall_extension("demo-ext")
            self.assertEqual(res3["status"], "ok")
            self.assertEqual(len(em.list_extensions()), 0)
            self.assertFalse((em.EXTENSIONS_DIR / "demo-ext").exists())


class TestGithubUrlParsing(unittest.TestCase):
    def test_repo_url(self):
        m = em._GITHUB_RE.match("https://github.com/owner/repo")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "owner")
        self.assertEqual(m.group(2), "repo")

    def test_subdir_url(self):
        m = em._GITHUB_RE.match("https://github.com/owner/repo/tree/main/plugins/foo")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(3), "main")
        self.assertEqual(m.group(4), "plugins/foo")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSingleDirWrapper(unittest.TestCase):
    """zip -r 产生的单层目录包装（finance-pack/manifest.yaml）必须可安装。"""

    def test_wrapped_zip_installs(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as td:
            zp = Path(td) / "wrapped.zip"
            zp.write_bytes(_make_zip({
                "finance-pack/manifest.yaml": MANIFEST,
                "finance-pack/plugin.py": PLUGIN_PY,
            }))
            res = em.install_extension(str(zp))
            self.assertEqual(res["status"], "ok", res)
            em.uninstall_extension("demo-ext")


class TestMarketplaceFetch(unittest.TestCase):
    def test_local_marketplace_file(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as td:
            mp = Path(td) / "marketplace.json"
            mp.write_text(json.dumps({
                "name": "test-market",
                "plugins": [{
                    "name": "x", "version": "1.0.0", "description": "d",
                    "author": {"name": "a"}, "category": "dev",
                    "source": {"type": "zip", "url": "https://example.com/x.zip", "sha256": "abc"},
                }],
            }))
            out = em.fetch_marketplace(str(mp))
            self.assertEqual(out["status"], "ok")
            self.assertEqual(len(out["plugins"]), 1)
            self.assertEqual(out["plugins"][0]["name"], "x")
            self.assertFalse(out["plugins"][0]["installed"])

    def test_missing_file_error(self):
        out = em.fetch_marketplace("/tmp/definitely-missing-marketplace.json")
        self.assertEqual(out["status"], "error")


class TestMCPClientTools(unittest.TestCase):
    """MCP stdio 桩工具：_rpc 帧协议级验证（不依赖外部服务器）。"""

    def test_sanitize_names(self):
        from mcp_client import sanitize_tool_name
        self.assertEqual(sanitize_tool_name("search-repo todo"), "search_repo_todo")
        self.assertEqual(sanitize_tool_name("123abc"), "t_123abc")
