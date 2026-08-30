"""Discovery Engine 测试：主动抓取 GitHub agent 技能。

网络隔离：mock 所有 httpx 调用，不真实打包抓取。聚焦核心逻辑：
- 树发现 / 指纹缓存 / 索引合并幂等 / 后端接入 merge / 状态
"""
import json
import sys
import tempfile
import unittest
import zipfile
import io
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import discovery


def _fake_tree(files):
    return [{"name": f, "hash": "x", "size": 10} for f in files]


def _fake_codeload_zip(files):
    """构造 fake 仓库 zip：repo-main/... 前缀。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in files:
            zf.writestr(f"repo-main/{f.lstrip('/')}", b"---\nname: my-skill\ndescription: a skill\n---\nbody")
    return buf.getvalue()


class _FakeResp:
    def __init__(self, text="", json_data=None, status=200, content=None):
        self._text = text
        self._json = json_data or {}
        self.status_code = status
        self.content = content or text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    @property
    def text(self):
        return self._text

    @property
    def is_success(self):
        return self.status_code < 400

    @property
    def content_bytes(self):
        return self.content

    def json(self):
        return self._json


class TestDiscoveryCore(unittest.TestCase):
    def setUp(self):
        # 隔离持久化文件
        self._tmp = tempfile.TemporaryDirectory()
        self._index = Path(self._tmp.name) / "index.json"
        self._cache = Path(self._tmp.name) / "cache.json"
        self._old_index = discovery.INDEX_FILE
        self._old_cache = discovery.CACHE_FILE
        discovery.INDEX_FILE = self._index
        discovery.CACHE_FILE = self._cache

    def tearDown(self):
        discovery.INDEX_FILE = self._old_index
        discovery.CACHE_FILE = self._old_cache
        self._tmp.cleanup()

    def test_has_skill_patterns(self):
        paths = ["/skills/a/SKILL.md", "/skills/b/skill.md", "/README.md"]
        specs = discovery._has_skill_patterns(paths)
        self.assertEqual(len(specs), 2)

    def test_fingerprint_stable(self):
        a = discovery._fingerprint(["/x/SKILL.md", "/y.md"])
        b = discovery._fingerprint(["/x/SKILL.md", "/y.md"])
        self.assertEqual(a, b)

    def test_parse_frontmatter(self):
        meta, body = discovery._parse_frontmatter("---\nname: foo\ndescription: bar\n---\nhi")
        self.assertEqual(meta["name"], "foo")
        self.assertEqual(body, "hi")

    def test_infer_permissions_shell_vs_readonly(self):
        self.assertEqual(discovery._infer_permissions({"requires": {"bins": ["x"]}}), ["shell", "network"])
        self.assertEqual(discovery._infer_permissions({}), ["readonly"])

    @mock.patch("discovery._jsdelivr_tree", return_value=["/skills/foo/SKILL.md"])
    @mock.patch("discovery._codeload_zip")
    def test_scan_repo_generates_entries(self, mock_zip, mock_tree):
        mock_zip.return_value = _fake_codeload_zip(["/skills/foo/SKILL.md"])
        entries = discovery._scan_repo("owner/repo", force=True)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repo"], "owner/repo")
        self.assertEqual(entries[0]["source_kind"], "openclaw-skill")
        self.assertIn("/skills/foo/SKILL.md", entries[0]["skill_path"])

    @mock.patch("discovery._jsdelivr_tree", return_value=["/skills/foo/SKILL.md"])
    @mock.patch("discovery._codeload_zip")
    def test_scan_fingerprint_skip(self, mock_zip, mock_tree):
        mock_zip.return_value = _fake_codeload_zip(["/skills/foo/SKILL.md"])
        discovery._scan_repo("owner/repo", force=True)  # 首次（写入指纹）
        # 二次（非 force）同一内容 → 指纹一致，返回 []（不生成条目）
        entries = discovery._scan_repo("owner/repo", force=False)
        self.assertEqual(entries, [])

    @mock.patch("discovery._jsdelivr_tree", return_value=[])
    def test_scan_no_skills(self, mock_tree):
        entries = discovery._scan_repo("owner/repo")
        self.assertEqual(entries, [])

    @mock.patch("discovery._jsdelivr_tree", return_value=None)
    def test_scan_tree_unreachable(self, mock_tree):
        entries = discovery._scan_repo("owner/repo")
        self.assertEqual(entries, [])


class TestDiscoveryApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_index = discovery.INDEX_FILE
        self._old_cache = discovery.CACHE_FILE
        discovery.INDEX_FILE = Path(self._tmp.name) / "idx.json"
        discovery.CACHE_FILE = Path(self._tmp.name) / "cach.json"

    def tearDown(self):
        discovery.INDEX_FILE = self._old_index
        discovery.CACHE_FILE = self._old_cache
        self._tmp.cleanup()

    def test_index_roundtrip_and_status(self):
        discovery._save_index({"repos": {"a": {"entries": 3}}, "entries": [{"repo": "a"}], "last_scan_ts": 123, "scan_stats": {"x": 1}})
        st = discovery.discover_status()
        self.assertEqual(st["repos"], 1)
        self.assertEqual(st["last_scan_ts"], 123)
        entries = discovery.get_discovered_entries()
        self.assertEqual(len(entries), 1)

    @mock.patch("discovery._collect_candidates")
    @mock.patch("discovery._scan_repo")
    def test_run_discovery_accumulates(self, mock_scan, mock_collect):
        mock_collect.return_value = {"owner/repo": {"stars": 10}, "other/repo": {"stars": 5}}
        mock_scan.side_effect = lambda repo, force=False: (
            [{"repo": repo, "skill_path": "/skills/a/SKILL.md", "description": "x"}]
            if repo == "owner/repo" else []
        )
        discovery.INDEX_FILE.write_text(json.dumps({"repos": {}, "entries": [], "last_scan_ts": 0, "scan_stats": {}}), encoding="utf-8")
        res = discovery.run_discovery(force=True, max_repos=3)
        self.assertEqual(res["entries"], 1)
        st = discovery.discover_status()
        self.assertEqual(st["repos"], 2)  # 两个都记录（无技能也登记）

    def test_fetch_all_merges_discovered(self):
        import extension_manager
        from unittest import mock
        discovery.INDEX_FILE.write_text(json.dumps({
            "repos": {"owner/repo": {"entries": 1}},
            "entries": [{"repo": "owner/repo", "skill_path": "/s/SKILL.md", "source_kind": "openclaw-skill"}],
            "last_scan_ts": 0, "scan_stats": {},
        }), encoding="utf-8")
        with mock.patch("extension_manager.list_market_sources", return_value=[]):
            out = extension_manager.fetch_all_markets()
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["plugins"][0]["market_source"], "GitHub 发现")


if __name__ == "__main__":
    unittest.main()


class TestDiscoveryImprovements(unittest.TestCase):
    """方向1完善：轮换、awesome、指纹内容感知。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_index = discovery.INDEX_FILE
        self._old_cache = discovery.CACHE_FILE
        discovery.INDEX_FILE = Path(self._tmp.name) / "idx.json"
        discovery.CACHE_FILE = Path(self._tmp.name) / "cach.json"

    def tearDown(self):
        discovery.INDEX_FILE = self._old_index
        discovery.CACHE_FILE = self._old_cache
        self._tmp.cleanup()

    def test_fingerprint_content_aware(self):
        paths = ["/skills/a/SKILL.md"]
        f1 = discovery._fingerprint(paths, {"/skills/a/SKILL.md": "aaa"})
        f2 = discovery._fingerprint(paths, {"/skills/a/SKILL.md": "bbb"})
        self.assertNotEqual(f1, f2)
        # 无内容 hash 时等价于路径指纹
        f3 = discovery._fingerprint(paths)
        self.assertEqual(f3, f1.split("-")[0])

    @mock.patch("discovery._jsdelivr_tree", return_value=["/README.md"])
    @mock.patch("discovery._read_file")
    def test_parse_awesome_links(self, mock_read, mock_tree):
        mock_read.return_value = (
            "参考：https://github.com/owner/repo-a 与 github.com/foo/bar.git "
            "以及 https://github.com/baz/qux/tree/main/x"
        )
        links = discovery.parse_awesome_links("VoltAgent/awesome-x")
        self.assertIn("owner/repo-a", links)
        self.assertIn("foo/bar", links)
        self.assertIn("baz/qux", links)
        self.assertNotIn("repo-a/tree", "".join(links.keys()))  # 不吞路径

    @mock.patch("discovery._collect_candidates")
    @mock.patch("discovery._scan_repo")
    def test_rotation_prefers_new_repos(self, mock_scan, mock_collect):
        """第二轮应优先扫未扫描仓库而非重复 top-星数。"""
        # 第一轮：已扫描 a/repo（高星），b/repo 未扫
        mock_collect.return_value = {
            "a/repo": {"stars": 1000},
            "b/repo": {"stars": 10},
        }
        mock_scan.return_value = []
        discovery.INDEX_FILE.write_text(json.dumps(
            {"repos": {"a/repo": {"stars": 1000, "entries": 0, "scanned": True}},
             "entries": [], "last_scan_ts": 0, "scan_stats": {}}), encoding="utf-8")
        # 断言第一轮新面孔 b/repo 排在前面（未扫描优先于已扫描高星）
        mock_scan.reset_mock()
        discovery.run_discovery(force=True, max_repos=5)
        scanned_order = [c[0][0] for c in mock_scan.call_args_list]
        self.assertEqual(scanned_order[0], "b/repo")
        self.assertIn("a/repo", scanned_order)

    @mock.patch("discovery._collect_candidates")
    @mock.patch("discovery._scan_repo")
    def test_incremental_scans_new_only(self, mock_scan, mock_collect):
        mock_collect.return_value = {
            "known/repo": {"stars": 500},
            "fresh/repo": {"stars": 20},
        }
        mock_scan.return_value = []
        discovery.INDEX_FILE.write_text(json.dumps(
            {"repos": {"known/repo": {"stars": 500, "scanned": True}},
             "entries": [], "last_scan_ts": __import__("time").time(), "scan_stats": {}}),
            encoding="utf-8")
        res = discovery.run_discovery(force=False, max_repos=5)  # 24h 内增量
        self.assertEqual(res.get("mode"), "incremental")
        scanned = [c[0][0] for c in mock_scan.call_args_list]
        self.assertEqual(scanned, ["fresh/repo"])


class TestMarketDedup(unittest.TestCase):
    """跨源去重：同一技能 (repo, skill_path) 只出现一次。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_index = discovery.INDEX_FILE
        self._old_cache = discovery.CACHE_FILE
        discovery.INDEX_FILE = Path(self._tmp.name) / "idx.json"
        discovery.CACHE_FILE = Path(self._tmp.name) / "cach.json"
        # 隔离 extension_manager 的 INSTALLED_FILE（避免污染真实安装记录）

    def tearDown(self):
        discovery.INDEX_FILE = self._old_index
        discovery.CACHE_FILE = self._old_cache
        self._tmp.cleanup()

    @mock.patch("extension_manager.list_market_sources")
    @mock.patch("extension_manager.fetch_marketplace")
    def test_fetch_all_dedup_same_skill(self, mock_fetch, mock_sources):
        """手动源 + 发现索引含同一技能 → 去重只出一条。"""
        import extension_manager
        # 发现索引写入一条技能
        discovery.INDEX_FILE.write_text(json.dumps({
            "repos": {"owner/repo": {"stars": 10}},
            "entries": [{"repo": "owner/repo", "skill_path": "/skills/x/SKILL.md",
                         "name": "x", "display_name": "X", "version": "1"}],
            "last_scan_ts": 0, "scan_stats": {}}), encoding="utf-8")
        mock_sources.return_value = [{
            "id": "manual", "name": "手动", "url": "https://github.com/owner/repo",
            "kind": "openclaw", "builtin": False}]
        mock_fetch.return_value = {"status": "ok", "plugins": []}
        # 手动源走 discover_auto 也返回同样技能
        import adapters
        with mock.patch("adapters.discover_auto", return_value={"status": "ok", "plugins": [
            {"repo": "owner/repo", "skill_path": "/skills/x/SKILL.md",
             "name": "x", "display_name": "X", "version": "1", "source_kind": "openclaw-skill"}]}):
            out = extension_manager.fetch_all_markets()
        self.assertEqual(out["count"], 1)  # 去重后只一条

    @mock.patch("extension_manager.list_market_sources")
    @mock.patch("extension_manager.fetch_marketplace")
    def test_fetch_all_installed_backfill(self, mock_fetch, mock_sources):
        import extension_manager
        discovery.INDEX_FILE.write_text(json.dumps({
            "repos": {"a/repo": {"stars": 5}},
            "entries": [{"repo": "a/repo", "skill_path": "/s/SKILL.md",
                         "name": "installed-skill", "version": "1"}],
            "last_scan_ts": 0, "scan_stats": {}}), encoding="utf-8")
        mock_sources.return_value = []
        # 模拟该技能已在 .installed.json
        with mock.patch("extension_manager._load_installed",
                        return_value={"extensions": [{"name": "installed-skill", "version": "1"}]}):
            out = extension_manager.fetch_all_markets()
        self.assertEqual(out["plugins"][0]["installed"], True)


class TestClaudePluginAndDeps(unittest.TestCase):
    """Claude 插件发现 + 外部依赖标注。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_index = discovery.INDEX_FILE
        discovery.INDEX_FILE = Path(self._tmp.name) / "idx.json"
        discovery.CACHE_FILE = Path(self._tmp.name) / "cach.json"

    def tearDown(self):
        discovery.INDEX_FILE = self._old_index
        self._tmp.cleanup()

    def _make_zip(self, with_plugin=True, with_skill=True):
        import io, json as j, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            if with_plugin:
                zf.writestr("repo-main/.claude-plugin/plugin.json", j.dumps(
                    {"name": "my-plugin", "version": "2.0.0",
                     "description": "A plugin", "author": {"name": "me"}}))
            if with_skill:
                zf.writestr("repo-main/skills/code/SKILL.md",
                            "---\nname: code\n---\n# CR\n")
        return buf.getvalue()

    @mock.patch("discovery._jsdelivr_tree")
    @mock.patch("discovery._codeload_zip")
    def test_claude_plugin_discovered(self, mock_zip, mock_tree):
        mock_tree.return_value = [".claude-plugin/plugin.json", "skills/code/SKILL.md"]
        mock_zip.return_value = self._make_zip()
        entries = discovery._scan_repo("me/plugin", force=True)
        kinds = [e["source_kind"] for e in entries]
        self.assertIn("claude-plugin", kinds)
        claude = next(e for e in entries if e["source_kind"] == "claude-plugin")
        self.assertEqual(claude["name"], "my-plugin")
        self.assertEqual(claude["version"], "2.0.0")

    @mock.patch("discovery._jsdelivr_tree")
    @mock.patch("discovery._codeload_zip")
    def test_external_deps_marked(self, mock_zip, mock_tree):
        mock_tree.return_value = ["skills/notion/SKILL.md"]
        buf = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("repo-main/skills/notion/SKILL.md",
                        "---\nname: notion\ndescription: x\nrequires:\n  bins: [ntask]\n---\nbody")
        mock_zip.return_value = buf.getvalue()
        entries = discovery._scan_repo("me/notion", force=True)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["external_deps"])

    def test_has_external_deps_variants(self):
        self.assertTrue(discovery._has_external_deps({"requires": {"bins": ["x"]}}))
        self.assertTrue(discovery._has_external_deps({"requires": {"anyBins": ["x"]}}))
        self.assertTrue(discovery._has_external_deps({"install": "brew install x"}))
        self.assertTrue(discovery._has_external_deps({"primaryEnv": "API_KEY"}))
        self.assertTrue(discovery._has_external_deps({"envVars": {"X": {"required": True}}}))
        self.assertFalse(discovery._has_external_deps({"name": "x", "description": "y"}))
