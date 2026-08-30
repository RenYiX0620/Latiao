"""生态格式适配器（adapters.py）测试：OpenClaw / Claude / 通用仓库。

网络策略隔离：真实 jsDelivr 是慢且不可控的依赖，本测试用 mock 捕获
httpx.get，不真实发请求（除了一个可选的真实探测用例, 默认跳过）。
"""
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import adapters
from adapters import (
    parse_github_repo,
    parse_frontmatter,
    discover_openclaw_tree,
    install_openclaw_skill,
    discover_claude_marketplace,
    install_claude_plugin,
    discover_auto,
)


def _fake_jsdelivr_tree(files):
    """返回 jsDelivr 树 API 响应字典。"""
    return [{"name": f, "hash": "x", "size": 10} for f in files]


class _FakeResp:
    """模拟 httpx.Response。"""
    def __init__(self, content: str = "", raw: bytes = None, status=200):
        self._content = content
        self._raw = raw
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def text(self):
        return self._content

    @property
    def content(self):
        return self._raw if self._raw is not None else self._content.encode("utf-8")

    def json(self):
        return json.loads(self._content)


class TestParseGithubRepo(unittest.TestCase):
    def test_full_url(self):
        self.assertEqual(
            parse_github_repo("https://github.com/21-DOT-DEV/openclaw-skills"),
            "21-dot-dev/openclaw-skills")

    def test_git_url(self):
        self.assertEqual(
            parse_github_repo("git@github.com:foo/bar-skill.git"),
            "foo/bar-skill")

    def test_shorthand(self):
        self.assertEqual(parse_github_repo("owner/repo"), "owner/repo")

    def test_invalid(self):
        self.assertIsNone(parse_github_repo("https://example.com/x"))
        self.assertIsNone(parse_github_repo(""))


class TestFrontmatter(unittest.TestCase):
    def test_parse(self):
        meta, body = parse_frontmatter("---\nname: foo\ndescription: hi\n---\ncontent here")
        self.assertEqual(meta["name"], "foo")
        self.assertEqual(meta["description"], "hi")
        self.assertEqual(body, "content here")

    def test_no_frontmatter(self):
        meta, body = parse_frontmatter("plain text")
        self.assertEqual(meta, {})
        self.assertEqual(body, "plain text")

    def test_multiline_desc(self):
        meta, _ = parse_frontmatter("---\ndescription: >\n  a\n  b\n---\nx")
        self.assertEqual(meta["description"].strip(), "a b")


class TestDiscoverOpenclaw(unittest.TestCase):
    def test_discovers_skill_mds(self):
        tree = _fake_jsdelivr_tree([
            "/skills/notion-cli/SKILL.md",
            "/skills/notion-task-skill/SKILL.md",
            "/skills/post-merge-pull/SKILL.md",
            "/skills/proton-mail-bridge/SKILL.md",
            "/README.md",
        ])
        with mock.patch("adapters._jsdelivr_tree", return_value=[t["name"] for t in tree]):
            with mock.patch("adapters._jsdelivr_file", return_value="---\nname: x\ndescription: y\n---\nbody"):
                r = discover_openclaw_tree("owner/skills-repo")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["count"], 4)
        first = r["plugins"][0]
        self.assertEqual(first["source_kind"], "openclaw-skill")
        self.assertIn("github.com/owner/skills-repo/tree/main/", first["source_url"])


class TestInstallOpenclaw(unittest.TestCase):
    def test_pack_valid_zip(self):
        with mock.patch("adapters._jsdelivr_tree",
                        return_value=["skills/my-skill/SKILL.md"]):
            with mock.patch("adapters._jsdelivr_file",
                            return_value="---\nname: my-skill\ndescription: test\ndescription: z\n---\nstep 1\nstep 2"):
                data = install_openclaw_skill("owner/repo", "skills/my-skill/SKILL.md")
        self.assertIsNotNone(data)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.yaml", names)
            self.assertTrue(any(n.endswith("SKILL.md") for n in names))
            manifest = zf.read("manifest.yaml").decode()
            self.assertIn("name: my-skill", manifest)

    def test_codeload_fallback(self):
        # jsDelivr 失败 → codeload 整仓裁剪
        with mock.patch("adapters._jsdelivr_file", return_value=None):
            with mock.patch("adapters._codeload_zip", return_value=b"not-zip"):
                data = install_openclaw_skill("owner/repo", "skills/x/SKILL.md")
        # codeload 内容非法 → 返回 None（安全失败，不崩溃）
        self.assertIsNone(data)


class TestDiscoverClaudeMarketplace(unittest.TestCase):
    def test_parse_marketplace(self):
        payload = json.dumps({
            "name": "test-market",
            "plugins": [
                {"name": "plugin-a", "version": "1.2.0", "description": "A plugin",
                 "source": {"url": "https://github.com/me/plugin-a"}},
            ],
        })
        with mock.patch("adapters._fetch_url", return_value=payload):
            r = discover_claude_marketplace("https://example.com/marketplace.json")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["plugins"][0]["source_kind"], "claude-plugin")


class TestInstallClaudePlugin(unittest.TestCase):
    def _make_repo_zip(self):
        # repo-main/.claude-plugin/plugin.json + skills/ + commands/ + agents/
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("repo-main/.claude-plugin/plugin.json", json.dumps({
                "name": "my-plugin", "version": "2.0.0",
                "description": "A Claude plugin", "author": {"name": "me"},
            }))
            zf.writestr("repo-main/skills/code-review/SKILL.md", "---\nname: code-review\n---\n## Code Review\n")
            zf.writestr("repo-main/commands/publish.md", "## Publish\n")
            zf.writestr("repo-main/agents/reviewer.md", "---\nname: reviewer\n---\n")
        return buf.getvalue()

    def test_filter_and_fallback(self):
        with mock.patch("adapters._codeload_zip", return_value=self._make_repo_zip()):
            data = install_claude_plugin("me/plugin-repo")
        self.assertIsNotNone(data)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.yaml", names)
            # skills 保持原结构映射
            skills = [n for n in names if n.startswith("skills/")]
            self.assertTrue(any("code-review" in n for n in skills))
            # commands 映射为技能
            self.assertTrue(any("publish" in n for n in skills))


class TestDiscoverAuto(unittest.TestCase):
    def test_auto_pick_openclaw(self):
        with mock.patch("adapters._jsdelivr_tree", return_value=[
            "/skills/a/SKILL.md", "/skills/b/SKILL.md"]):
            with mock.patch("adapters._jsdelivr_file",
                            return_value="---\nname: a\n---\nbody"):
                r = discover_auto("https://github.com/owner/repo")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["count"], 2)

    def test_invalid_repo(self):
        self.assertEqual(discover_auto("not-a-repo").get("status"), "error")


if __name__ == "__main__":
    unittest.main()
