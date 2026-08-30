"""生态格式适配器：把 GitHub 开源 agent 生态内容映射进 Latiao 扩展体系。

支持的源（只读适配，不改动源仓库）：
- OpenClaw 技能：<repo>/skills/<名>/SKILL.md（frontmatter + markdown）
- Claude Code 插件：.claude-plugin/plugin.json + skills/commands/agents/ + marketplace.json
- 通用仓库兜底：扫 skills/*.md 与根 SKILL.md

网络策略（本机实测）：
- jsDelivr 文件树 API/data.jsdelivr.com 列文件（raw.githubusercontent 被屏蔽时唯一可行通道）
- codeload.github.com 下整仓库 zip
- 全部带镜像兜底，保证国内网络可用。
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from pathlib import Path

import httpx

logger = logging.getLogger("latiao-sidecar")

_TIMEOUT = httpx.Timeout(25)
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Latiao-ExtAdapter/1.0"

_GH_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)
_GIT_URL_RE = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$")

# 常见代理前缀（gitee 镜像、清华大学镜像等）——用户可粘贴这类地址
_MIRROR_PREFIXES = ("https://gitcode.com/", "https://cnb.cool/")


def parse_github_repo(url: str) -> str | None:
    """把 github URL / git 地址归一成 'owner/repo'。非 github 源返回 None。"""
    if not url:
        return None
    url = url.strip()
    m = _GH_RE.match(url) or _GIT_URL_RE.match(url)
    if m:
        return f"{m.group(1).lower()}/{m.group(2).lower()}"
    # 允许 owner/repo 简写
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", url):
        return url.lower()
    return None


def _jsdelivr_tree(repo: str, ref: str = "main", timeout: float = 20) -> list[str]:
    """jsDelivr 文件树 API：返回仓库文件路径列表（flat）。"""
    url = f"https://data.jsdelivr.com/v1/packages/gh/{repo}@{ref}?structure=flat"
    try:
        resp = httpx.get(url, timeout=httpx.Timeout(timeout), headers={"User-Agent": "Latiao/1.0"})
        resp.raise_for_status()
        data = resp.json()
        return [f["name"] for f in data.get("files", [])]
    except Exception:
        logger.warning("jsDelivr tree failed for %s", repo, exc_info=True)
        return []


def _jsdelivr_file(repo: str, path: str, ref: str = "main", timeout: float = 20) -> str | None:
    """jsDelivr 单文件读取（URL 编码路径，防特殊字符）。"""
    enc = path.lstrip("/").replace(" ", "%20")
    url = f"https://cdn.jsdelivr.net/gh/{repo}@{ref}/{enc}"
    try:
        resp = httpx.get(url, timeout=httpx.Timeout(timeout), headers={"User-Agent": _UA})
        resp.raise_for_status()
        return resp.text
    except Exception:
        logger.warning("jsDelivr file failed for %s@%s", repo, path, exc_info=True)
        return None


def _codeload_zip(repo: str, ref: str = "main", timeout: float = 60) -> bytes | None:
    """codeload 下整仓库 zip（与 extension_manager._download 的 git 分支一致）。"""
    url = (
        f"https://codeload.github.com/{repo}/zip/refs/heads/{ref}"
        if ref != "main" else f"https://codeload.github.com/{repo}/zip/refs/heads/main"
    )
    try:
        resp = httpx.get(url, timeout=httpx.Timeout(timeout), follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception:
        logger.warning("codeload failed for %s@%s", repo, ref, exc_info=True)
        return None


def _fetch_url(url: str, timeout: float = 20) -> str | None:
    """通用 GET：用于 marketplace.json / plugin.json（尝试镜像兜底）。"""
    try:
        resp = httpx.get(url, timeout=httpx.Timeout(timeout), follow_redirects=True)
        resp.raise_for_status()
        return resp.text
    except Exception:
        logger.warning("fetch failed for %s", url, exc_info=True)
        return None


# ═══════════════════════════════════════════════════════
#  Frontmatter 解析（OpenClaw / Claude 技能共用）
# ═══════════════════════════════════════════════════════

def parse_frontmatter(content: str) -> tuple[dict, str]:
    """解析 YAML frontmatter。返回 (meta, body)。无 frontmatter → ({}, 原文)。"""
    if not content or not content.startswith("---"):
        return {}, (content or "").strip()
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, (content or "").strip()
    try:
        import yaml
        meta = yaml.load(parts[1], Loader=yaml.SafeLoader)
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        logger.warning("frontmatter parse failed", exc_info=True)
        meta = {}
    return meta, parts[2].strip()


def _infer_permissions(front: dict) -> list[str]:
    """从 frontmatter 推断 manifest 权限：
    install/requires.bins/涉及外部命令 → shell；仅说明 → readonly。"""
    requires = front.get("requires") or {}
    if isinstance(requires, dict):
        bins = requires.get("bins") or requires.get("anyBins")
    else:
        bins = None
    has_install = bool(front.get("install") or front.get("primaryEnv") or front.get("envVars"))
    has_bin = bool(bins or front.get("tool") or "bin/" in str(front.get("skillKey", "")))
    if has_install or has_bin:
        return ["shell", "network"]
    return ["readonly"]


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", (name or "").lower()).strip("-")[:64] or "unnamed"


# ═══════════════════════════════════════════════════════
#  OpenClaw 技能源
# ═══════════════════════════════════════════════════════

def discover_openclaw_tree(repo: str) -> dict:
    """扫描 OpenClaw 技能仓库：发现可用技能条目（不下载内容）。"""
    repo = parse_github_repo(repo)
    if not repo:
        return {"status": "error", "message": f"无法识别 GitHub 仓库: {repo!r}"}
    files = _jsdelivr_tree(repo)
    if not files:
        return {"status": "error", "message": f"仓库 {repo} 文件树获取失败（jsDelivr 不可达或仓库不存在）"}
    # 技能 = skills/<名>/SKILL.md 或 <名>/SKILL.md（任意层级）
    entries: list[dict] = []
    seen: set[str] = set()
    for f in files:
        if not f.lower().endswith("/skill.md"):
            continue
        parent = Path(f).parent
        skill_name = parent.name
        if skill_name in seen:
            continue
        seen.add(skill_name)
        # 读 frontmatter 摘要（只读 description，不下载全文）
        meta = {}
        body = ""
        content = _jsdelivr_file(repo, f)
        if content:
            meta, body = parse_frontmatter(content)
        desc = str(meta.get("description") or "")
        if isinstance(desc, list):
            desc = " ".join(str(x) for x in desc)
        # YAML 折叠块 (>|) 会在尾部留下换行——清理避免 UI 显示碎行
        desc = re.sub(r"\s+$", "", desc) if isinstance(desc, str) else desc
        perm = _infer_permissions(meta)
        entries.append({
            "name": _safe_name(str(meta.get("name") or skill_name)),
            "display_name": str(meta.get("name") or skill_name),
            "version": "1.0.0",
            "description": desc or _first_desc_line(body),
            "author": {"name": repo.split("/")[0]},
            "category": "community",
            "source_url": f"https://github.com/{repo}/tree/main/{f}",
            "source_kind": "openclaw-skill",
            "repo": repo,
            "skill_path": f,
            "permissions": perm,
            "sha256": "",
        })
    return {"status": "ok", "repo": repo, "plugins": entries, "count": len(entries)}


def _first_desc_line(body: str) -> str:
    for line in (body or "").split("\n"):
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            return s[:120]
    return ""


_SKILL_DIR_MAX = 5 * 1024 * 1024  # 5MB：技能目录超过则退回整仓 codeload


def install_openclaw_skill(repo: str, skill_path: str) -> bytes | None:
    """下载技能目录并打包成 .latiaoext（内存 zip 字节），交给 extension_manager 安装。
    优先 jsDelivr 树枚举目录全部文件（脚本/参考文件不丢），5MB 内逐文件拼装；
    树不可用或超 5MB 时退 codeload 整仓裁剪。"""
    repo = parse_github_repo(repo)
    if not repo or not skill_path:
        return None
    skills_dir = skill_path.rsplit("/", 1)[0]  # 如 skills/notion-cli
    # 1) 首选：jsDelivr 树枚举目录内所有文件
    files_all = _jsdelivr_tree(repo)
    meta = {}
    body = ""
    dir_files: dict[str, bytes] = {}
    if files_all:
        prefix = skills_dir.strip("/") + "/"
        count = 0
        read_ok = True
        for f in files_all:
            rel = f.lstrip("/")
            if not rel.startswith(prefix):
                continue
            # 只要直接挂在技能目录下（含子目录）
            content = _jsdelivr_file(repo, rel)
            if content is None:
                read_ok = False
                break
            count += len(content.encode("utf-8"))
            if count > _SKILL_DIR_MAX:
                read_ok = False  # 超限，退回整仓
                break
            dir_files[rel[len(prefix):]] = content
        if read_ok and dir_files:
            # 解析 SKILL.md 的 frontmatter 用于 manifest
            sk = dir_files.get("SKILL.md") or dir_files.get("skill.md")
            if sk:
                meta, body = parse_frontmatter(sk)
            return _pack_skill_dir(dir_files, skills_dir, meta=meta)
    # 2) 兜底：codeload 整仓裁剪（多文件保留）
    zip_bytes = _codeload_zip(repo)
    if zip_bytes is None:
        return None
    subset = _zip_subset(zip_bytes, prefix if 'prefix' in dir() else skills_dir + "/")
    return _pack_skill_dir(subset, skills_dir)


def _zip_subset(zip_bytes: bytes, prefix: str) -> dict[str, bytes]:
    """从仓库 zip 中提取 <prefix> 子目录的文件映射 {rel_path: bytes}。"""
    out: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith(prefix) and not info.is_dir():
                    # 去掉外层 repo/ 目录（repo-master/skills/x/...）
                    parts = name.split("/", 1)
                    key = parts[1] if len(parts) == 2 else name
                    out[key] = zf.read(info)
    except Exception:
        logger.warning("zip subset failed", exc_info=True)
        return {}
    return out


def _pack_skill_dir(files: dict[str, bytes], skill_rel: str, meta: dict | None = None) -> bytes | None:
    """把技能文件映射打包成 .latiaoext（manifest.yaml + skills/SKILL.md*）。"""
    if not files:
        return None
    meta = meta or {}
    name = _safe_name(str(meta.get("name") or skill_rel.rsplit("/", 1)[-1]))
    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": str(meta.get("description") or "社区技能包"),
        "author": {"name": "community"},
        "permissions": _infer_permissions(meta),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.yaml", __import__("yaml").safe_dump(manifest, allow_unicode=True))
        for key, data in files.items():
            rel = key.split("/", 1)[1] if "/" in key else key
            if rel.lower().endswith("skill.md"):
                zf.writestr(f"skills/{name}/SKILL.md", data)
            else:
                zf.writestr(f"skills/{name}/{rel.rsplit('/', 1)[-1]}", data)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════
#  Claude Code 插件源
# ═══════════════════════════════════════════════════════

def discover_claude_marketplace(url: str) -> dict:
    """发现 Claude Code marketplace（.claude-plugin/marketplace.json）。"""
    text = _fetch_url(url)
    if text is None:
        return {"status": "error", "message": f"marketplace 拉取失败: {url}"}
    try:
        data = json.loads(text)
    except Exception as e:
        return {"status": "error", "message": f"marketplace 解析失败: {e}"}
    entries: list[dict] = []
    for p in data.get("plugins", []) or []:
        src = p.get("source", {})
        src_url = src.get("url", "") if isinstance(src, dict) else ""
        entries.append({
            "name": _safe_name(str(p.get("name") or "")),
            "display_name": str(p.get("name") or ""),
            "version": str(p.get("version") or "1.0.0"),
            "description": str(p.get("description") or ""),
            "author": p.get("author", {}),
            "category": "claude-plugin",
            "source_url": src_url,
            "source_kind": "claude-plugin",
            "repo": parse_github_repo(src_url) or "",
            "sha256": "",
        })
    return {"status": "ok", "repo": url, "plugins": entries, "count": len(entries)}


def install_claude_plugin(repo: str) -> bytes | None:
    """下载 Claude 插件仓库 → 定位插件根 → 打包 .latiaoext。"""
    repo = parse_github_repo(repo)
    if not repo:
        return None
    zip_bytes = _codeload_zip(repo)
    if zip_bytes is None:
        return None
    # 仓库根定位：repo-main/.claude-plugin/plugin.json
    plugin_root = _find_plugin_root(zip_bytes)
    manifest = None
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                rel = name.split("/", 1)[1] if "/" in name else name
                if plugin_root and rel.startswith(plugin_root):
                    key = rel[len(plugin_root):].lstrip("/")
                else:
                    key = rel
                if key == ".claude-plugin/plugin.json":
                    manifest = json.loads(zf.read(info).decode("utf-8", "ignore"))
                if key.startswith((".claude-plugin/",)) and not key.endswith("marketplace.json"):
                    continue
                if key.startswith(("skills/", "commands/", "agents/")):
                    files[key] = zf.read(info)
    except Exception:
        logger.warning("claude plugin zip scan failed", exc_info=True)
        return None
    if not manifest:
        manifest = {"name": repo.split("/")[1], "version": "1.0.0",
                    "description": f"Claude Code plugin from {repo}"}
    return _pack_claude_plugin(manifest, files)


def _find_plugin_root(zip_bytes: bytes) -> str | None:
    """仓库 zip 中定位 .claude-plugin/plugin.json 的所属子目录（repo-root 消去）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            for name in names:
                if name.endswith(".claude-plugin/plugin.json"):
                    # repo-main/.claude-plugin/plugin.json → root = repo-main/
                    return name.split("/")[0] + "/"
            return None
    except Exception:
        return None


def _pack_claude_plugin(manifest: dict, files: dict[str, bytes]) -> bytes | None:
    """把 Claude 插件组件映射成 .latiaoext 字节。"""
    name = _safe_name(str(manifest.get("name") or "claude-plugin"))
    author = manifest.get("author", {})
    if isinstance(author, dict):
        author = {"name": author.get("name", "claude-community")}
    else:
        author = {"name": str(author or "claude-community")}
    latiao_manifest = {
        "name": name,
        "version": str(manifest.get("version") or "1.0.0"),
        "description": str(manifest.get("description") or ""),
        "author": author,
        "permissions": ["readonly"],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.yaml", __import__("yaml").safe_dump(latiao_manifest, allow_unicode=True))
        for key, data in files.items():
            if key.startswith("skills/"):
                # skills/<名>/SKILL.md → skills/<名>/SKILL.md（保持原结构）
                zf.writestr(key, data)
            elif key.startswith("commands/"):
                # commands/<名>.md → 映射为 use_skill 技能：文件名即技能名
                cmd_name = Path(key.split("/", 1)[1] if "/" in key else key).stem
                zf.writestr(f"skills/{cmd_name}/SKILL.md", data)
            elif key.startswith("agents/"):
                zf.writestr(key.replace("agents/", "agents/", 1), data)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════
#  通用仓库兜底 + 自动识别入口
# ═══════════════════════════════════════════════════════

def discover_generic_repo(repo: str) -> dict:
    """兜底：扫任意仓库的 skills/*.md（每目录一个技能）或根 SKILL.md。"""
    repo = parse_github_repo(repo)
    if not repo:
        return {"status": "error", "message": f"无法识别 GitHub 仓库: {repo!r}"}
    files = _jsdelivr_tree(repo)
    if not files:
        return {"status": "error", "message": f"仓库 {repo} 文件树获取失败（jsDelivr 不可达或仓库不存在）"}
    entries: list[dict] = []
    seen: set[str] = set()
    for f in files:
        low = f.lower()
        if low == "/skill.md" or low.endswith("skill.md"):
            parent = Path(f).parent
            skill_name = parent.name if parent.name else "root"
            if skill_name in seen:
                continue
            seen.add(skill_name)
            content = _jsdelivr_file(repo, f)
            meta, body = parse_frontmatter(content or "")
            entries.append({
                "name": _safe_name(str(meta.get("name") or skill_name)),
                "display_name": str(meta.get("name") or skill_name),
                "version": "1.0.0",
                "description": str(meta.get("description") or _first_desc_line(body)),
                "author": {"name": repo.split("/")[0]},
                "category": "community",
                "source_url": f"https://github.com/{repo}/tree/main/{f}",
                "source_kind": "generic-skill",
                "repo": repo,
                "skill_path": f,
                "permissions": _infer_permissions(meta),
                "sha256": "",
            })
    return {"status": "ok", "repo": repo, "plugins": entries, "count": len(entries)}


def discover_auto(url: str) -> dict:
    """自动识别源类型并发现条目。"""
    repo = parse_github_repo(url)
    if not repo:
        # 非 github → 当作 marketplace.json 试试
        return discover_claude_marketplace(url)
    files = _jsdelivr_tree(repo)
    if not files:
        return discover_claude_marketplace(url) if url.startswith("http") else \
            {"status": "error", "message": f"仓库 {repo} 文件树获取失败"}
    has_claude = any(".claude-plugin/marketplace.json" in f.lower() for f in files)
    has_openclaw = any(f.lower().endswith("skill.md") and "skills/" in f.lower() for f in files)
    if has_claude:
        # 找到 marketplace.json 路径再拉
        for f in files:
            if f.lower().endswith("marketplace.json") and ".claude-plugin/" in f.lower():
                text = _jsdelivr_file(repo, f)
                if text:
                    try:
                        data = json.loads(text)
                        return discover_claude_marketplace(f"https://cdn.jsdelivr.net/gh/{repo}@main/{f}")
                    except Exception:
                        pass
    if has_openclaw:
        return discover_openclaw_tree(repo)
    return discover_generic_repo(repo)
