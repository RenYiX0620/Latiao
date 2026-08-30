"""Discovery Engine — 主动抓取 GitHub 上的 agent 技能/插件，映射进 Latiao 市场。

两层设计：
1. 【仓库发现】repo search（匿名 10 次/分）→ 候选仓库清单；
   git trees API（core 60/h，1 请求/仓库列全树）→ 存在 skills/*/SKILL.md 与否
2. 【内容读取】jsDelivr 优先（无配额，已镜像仓库）→ frontmatter → 条目；
   raw.githubusercontent 兜底（core 配额内）

索引持久化：~/.local-ai-os/discovered_index.json
指纹缓存：   ~/.local-ai-os/discovery_cache.json（文件 hash 拼接 md5，跳过未变更仓库）

配额硬约束（本机实测）：
- repo search 10/分（每轮 6 次 = 60 仓库，节流 8s）
- core 60/h（git trees + raw 文件共用一个池，已用 5）
- jsDelivr 树/文件无限额（免费镜像，优先）

走"cat 目录"双通道：awesome 列表仓库直接解析 README 里的技能仓库链接。
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from pathlib import Path

import httpx

logger = logging.getLogger("latiao-sidecar")

_PROGRESS_DIR = Path.home() / ".local-ai-os"
INDEX_FILE = _PROGRESS_DIR / "discovered_index.json"
CACHE_FILE = _PROGRESS_DIR / "discovery_cache.json"

_GH_TOKEN = os.environ.get("DISCOVERY_GH_TOKEN", "")  # 可选：提高配额

# ── TLS 信任链（本机关键：Watt Toolkit/SteamTools MITM CA 在系统 keychain 但
#    不在 certifi——从 keychain 导出生成 trust bundle，与 curl 行为一致） ──
_CA_BUNDLE = _PROGRESS_DIR / ".ca_trust.pem"


def _system_ca_bundle() -> str | None:
    """导出系统 keychain 信任的 CA 到临时 bundle，返回路径；失败返回 None。"""
    if _CA_BUNDLE.exists() and _CA_BUNDLE.stat().st_size > 1000:
        return str(_CA_BUNDLE)
    try:
        # 从 System keychain 导出全部信任的 CA(含 SteamTools)，追加到默认 CA
        import subprocess
        out = subprocess.run(
            ["security", "find-certificate", "-a", "-p", "/Library/Keychains/System.keychain"],
            capture_output=True, text=True, timeout=20,
        )
        extra = out.stdout or ""
        # 合并：certifi 默认 CA + keychain 附加 CA
        import certifi
        base = Path(certifi.where()).read_text(encoding="utf-8")
        combined = base + "\n" + extra if extra else base
        _CA_BUNDLE.write_text(combined, encoding="utf-8")
        return str(_CA_BUNDLE)
    except Exception:
        logger.debug("system CA export failed", exc_info=True)
        return None


def _gh_headers() -> dict:
    h = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    if _GH_TOKEN:
        h["Authorization"] = f"Bearer {_GH_TOKEN}"
    return h


def _verify() -> str | bool:
    return _system_ca_bundle() or True

# ── 搜索查询（每轮 6 次配额，命中主流生态） ──
SEARCH_QUERIES = [
    "openclaw skills",
    "claude skills in:name",
    "claude plugins",
    "agent skills",
    "claude-code skills",
    "mcp skills",
]

# 已知组织（core 配额富余时补充，避免漏掉未出现在搜索的）
ORG_NAMES = ["openclaw", "anthropics"]

# ═══════════════════════════════════════════════════════
#  HTTP 层（全走 jsDelivr + GitHub API，本机实测全部可达）
# ═══════════════════════════════════════════════════════

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) Latiao-Discovery/1.0"
_TIMEOUT = httpx.Timeout(20)
_SEARCH_T = 12   # repo search（10/分，拖死会浪费配额）
_GH_T = 15       # core org/trees
_FILE_T = 18     # jsDelivr/raw 读文件


def _gh_headers() -> dict:
    h = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    if _GH_TOKEN:
        h["Authorization"] = f"Bearer {_GH_TOKEN}"
    return h


def _gh_get(url: str, timeout: float = 15):
    """GitHub API GET（core 池）。返回 json 或 None。"""
    try:
        resp = httpx.get(url, headers=_gh_headers(), timeout=httpx.Timeout(timeout), verify=_verify())
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            logger.warning("GitHub core 配额已耗尽: %s", url)
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("gh get failed %s: %s", url, e)
        return None


def search_repositories(query: str, per_page: int = 10, page: int = 1) -> list[dict]:
    """repo search 一页。返回仓库 dict 列表。"""
    try:
        resp = httpx.get(
            "https://api.github.com/search/repositories",
            headers=_gh_headers(), timeout=httpx.Timeout(_SEARCH_T), verify=_verify(),
            params={"q": query, "per_page": per_page, "page": page, "sort": "stars", "order": "desc"},
        )
        if resp.status_code == 403 and "rate" in resp.text.lower():
            logger.warning("repo search 配额耗尽（匿名 10/分）")
            return []
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception as e:
        logger.warning("repo search failed %s: %s", query, e)
        return []


def org_repos(org: str, per_page: int = 30) -> list[dict]:
    """组织仓库列表（core 配额）。"""
    try:
        resp = httpx.get(
            f"https://api.github.com/orgs/{org}/repos",
            headers=_gh_headers(), timeout=httpx.Timeout(_GH_T), verify=_verify(), params={"per_page": per_page, "sort": "updated"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("org repos failed %s: %s", org, e)
        return []


def _git_tree(repo: str, ref: str = "main") -> list[str] | None:
    """git trees API：一次拉全树路径列表（core 配额，1 请求/仓库）。"""
    data = _gh_get(f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1")
    if data is None:
        return None
    paths = [t.get("path") for t in data.get("tree", []) if isinstance(t, dict)]
    if not paths and data.get("truncated"):
        logger.warning("tree truncated for %s", repo)
    return paths or None


def _has_skill_patterns(paths: list[str]) -> list[dict]:
    """从树路径提取技能条目。返回 [{skill_path, parent}]。"""
    out = []
    for p in paths or []:
        if p and p.lower().endswith("skill.md"):
            parent = p.rsplit("/", 1)[0]
            out.append({"skill_path": p, "parent": parent or ""})
    return out


def _jsdelivr_tree(repo: str, ref: str = "main") -> list[str] | None:
    """jsDelivr 树 API（无限额、秒级，已镜像仓库）：返回 flat 路径列表。失败 None。"""
    try:
        resp = httpx.get(
            f"https://data.jsdelivr.com/v1/packages/gh/{repo}@{ref}?structure=flat",
            headers={"User-Agent": _UA}, timeout=httpx.Timeout(15), verify=_verify(),
        )
        if resp.is_success:
            return [f["name"] for f in resp.json().get("files", [])]
    except Exception:
        pass
    return None


def _codeload_zip(repo: str, ref: str = "main") -> bytes | None:
    """codeload 下整仓 zip（一次下载，1-16s，无重定向坑）。失败 None。"""
    try:
        resp = httpx.get(
            f"https://codeload.github.com/{repo}/zip/refs/heads/{ref}",
            headers={"User-Agent": _UA}, timeout=httpx.Timeout(60), follow_redirects=True,
            verify=_verify(),
        )
        # codeload 可能 301 到其他 CDN，follow_redirects 已处理；内容过大时截断防内存
        if resp.is_success and len(resp.content) < 300 * 1024 * 1024:
            return resp.content
    except Exception:
        logger.debug("codeload failed %s", repo, exc_info=True)
    return None


def _read_file(repo: str, path: str) -> str | None:
    """读单文件：jsDelivr 优先（无限额），raw 兜底（core）。"""
    # jsDelivr
    try:
        enc = path.lstrip("/").replace(" ", "%20")
        resp = httpx.get(f"https://cdn.jsdelivr.net/gh/{repo}@main/{enc}",
                         headers={"User-Agent": _UA}, timeout=httpx.Timeout(_FILE_T), verify=_verify())
        if resp.is_success and len(resp.text) > 10:
            return resp.text
    except Exception:
        pass
    # raw 兜底
    try:
        resp = httpx.get(f"https://raw.githubusercontent.com/{repo}/main/{path}",
                         headers=_gh_headers(), timeout=httpx.Timeout(_FILE_T), verify=_verify())
        if resp.is_success:
            return resp.text
    except Exception:
        pass
    return None


def _parse_frontmatter(content: str) -> tuple[dict, str]:
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
        meta = {}
    return meta, parts[2].strip()


def _infer_permissions(front: dict) -> list[str]:
    requires = front.get("requires") or {}
    if isinstance(requires, dict):
        bins = requires.get("bins") or requires.get("anyBins")
    else:
        bins = None
    has_install = bool(front.get("install") or front.get("primaryEnv") or front.get("envVars"))
    if has_install or bins or "bin/" in str(front.get("skillKey", "")):
        return ["shell", "network"]
    return ["readonly"]


def _first_desc_line(body: str) -> str:
    for line in (body or "").split("\n"):
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            return s[:120]
    return ""


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", (name or "").lower()).strip("-")[:64] or "unnamed"


_AWESOME_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/|\s|$|[)\]}])",
    re.I,
)


def parse_awesome_links(repo: str, ref: str = "main") -> dict[str, int]:
    """解析 awesome/curated 类仓库（README.md + categories/*.md）里的 github 链接。
    返回 {owner/repo: 未知星数(0)}。用 jsDelivr 读文件（无限额）。"""
    out: dict[str, int] = {}
    files = _jsdelivr_tree(repo, ref)
    if not files:
        return {}
    targets = [f for f in files if f.lower().endswith(("readme.md", ".md")) and "/categories/" in f.lower() or f.lower().endswith("readme.md")]
    # 优先 README + categories 下的 md（各取前 6 个，配额理性）
    targets = targets[:8]
    for f in targets:
        text = _read_file(repo, f)
        if not text:
            continue
        for m in _AWESOME_LINK_RE.finditer(text):
            owner, name = m.group(1).lower(), m.group(2).lower()
            if owner in ("www", "raw", "cdn", "github") or name in ("", "blob", "tags", "issues", "pulls", "releases"):
                continue
            full = f"{owner}/{name}"
            if full != repo.lower() and full not in out:
                out[full] = 0
        if len(out) >= 40:
            break
    return out


def _fingerprint(paths: list[str], content_hashes: dict[str, str] | None = None) -> str:
    """仓库指纹：路径列表 md5 ＋ 首 8 个 SKILL.md 内容 hash 摘要。
    内容变更（同路径不同内容）也会改变指纹，触发增量重扫。"""
    base = hashlib.md5("\n".join(sorted(paths or [])).encode("utf-8")).hexdigest()[:16]
    if content_hashes:
        # 按路径排序取前 8 个 SKILL.md 内容的 md5 拼接
        md5s = [content_hashes[k] for k in sorted(content_hashes.keys())[:8]]
        if md5s:
            base += "-" + hashlib.md5("".join(md5s).encode("utf-8")).hexdigest()[:12]
    return base


# ═══════════════════════════════════════════════════════
#  索引 / 缓存持久化
# ═══════════════════════════════════════════════════════

_lock = threading.Lock()


def _load_index() -> dict:
    try:
        if INDEX_FILE.exists():
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("discovered index load failed", exc_info=True)
    return {"repos": {}, "entries": [], "last_scan_ts": 0, "scan_stats": {}}


def _save_index(index: dict):
    try:
        _PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(INDEX_FILE)
    except Exception:
        logger.warning("discovered index save failed", exc_info=True)


def _load_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(cache, dict):
                return cache
    except Exception:
        pass
    return {"fingerprints": {}}


def _save_cache(cache: dict):
    try:
        _PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
#  核心抓取
# ═══════════════════════════════════════════════════════

_CANDIDATES: dict[str, dict] = {}  # repo -> {stars, updated_at, reason}


def _collect_candidates() -> dict[str, dict]:
    """仓库发现：repo search（6 查询 × 翻 2 页 × 50）+ 组织 + awesome 列表链接。"""
    out: dict[str, dict] = {}
    for q in SEARCH_QUERIES:
        for page in (1, 2):  # 翻 2 页扩候选（每页 50，配额允许）
            repos = search_repositories(q, per_page=50, page=page)
            for r in repos:
                full = r.get("full_name", "")
                if not full:
                    continue
                if full not in out:
                    out[full] = {"stars": r.get("stargazers_count", 0), "reason": f"search:{q[:18]}"}
            if not repos:
                break
            time.sleep(1.5)  # 节流 search 配额
    for org in ORG_NAMES:
        for r in org_repos(org):
            full = r.get("full_name", "")
            if full:
                out.setdefault(full, {"stars": r.get("stargazers_count", 0), "reason": f"org:{org}"})
    # awesome/curated 类仓库：解析其 README/categories 里的 github 链接，并入候选池
    known_awesome = [repo for repo, meta in out.items()
                     if any(k in repo.lower() for k in ("awesome", "curated", "collection"))]
    known_awesome = known_awesome[:8]  # 限 8 个，避免配额爆炸
    for repo in known_awesome:
        linked = parse_awesome_links(repo)
        for full, stars in linked.items():
            out.setdefault(full, {"stars": stars or out.get(full, {}).get("stars", 0),
                                  "reason": f"awesome:{repo.split('/')[1][:18]}"})
    return out


def _has_external_deps(front: dict) -> bool:
    """检测技能是否依赖外部工具/CLI/API（requires.bins/install/primaryEnv/envVars）。"""
    requires = front.get("requires") or {}
    if isinstance(requires, dict):
        if requires.get("bins") or requires.get("anyBins"):
            return True
    return bool(front.get("install") or front.get("primaryEnv") or front.get("envVars"))


def _scan_claude_plugin(repo: str, zip_files: dict[str, bytes]) -> list[dict]:
    """从仓库 zip 中解析 .claude-plugin/plugin.json 生成 Claude 插件条目。
    支持多插件（external_plugins/<名>/ 下每个 .claude-plugin/plugin.json 各一条）。"""
    out: list[dict] = []
    plugin_files = [n for n in zip_files if n.endswith(".claude-plugin/plugin.json")]
    if not plugin_files:
        return []
    seen: set[str] = set()
    for n in plugin_files:
        try:
            data = json.loads(zip_files[n].decode("utf-8", "ignore"))
        except Exception:
            logger.warning("claude plugin.json parse failed %s:%s", repo, n, exc_info=True)
            continue
        if not isinstance(data, dict) or not data.get("name"):
            continue
        pname = _safe_name(str(data.get("name")))
        if pname in seen:
            continue
        seen.add(pname)
        author = data.get("author", {})
        if isinstance(author, dict):
            author = {"name": author.get("name", repo.split("/")[0])}
        else:
            author = {"name": str(author or repo.split("/")[0])}
        out.append({
            "name": pname,
            "display_name": str(data.get("name")),
            "version": str(data.get("version") or "1.0.0"),
            "description": str(data.get("description") or "Claude Code 插件"),
            "author": author,
            "category": "claude-plugin",
            "source_url": f"https://github.com/{repo}",
            "source_kind": "claude-plugin",
            "repo": repo,
            "skill_path": "",
            "permissions": ["readonly"],
            "sha256": "",
            "external_deps": False,
        })
    return out


def _scan_repo(repo: str, force: bool = False) -> list[dict]:
    """扫描单个仓库：jsDelivr 树（秒级，判有无技能）→ codeload 整仓 zip（1-15s）
    → 本地解压抽 SKILL.md → 条目；同时识别 .claude-plugin/plugin.json → Claude 插件条目。"""
    paths = _jsdelivr_tree(repo)
    if paths is None:
        return []
    entries_spec = _has_skill_patterns(paths)
    has_claude = any(".claude-plugin/plugin.json" in p.lower() for p in paths)
    if not entries_spec and not has_claude:
        return []
    # 先取内容相关指纹：路径指纹 + 已缓存内容 hash（无内容 hash 时用路径指纹先判）
    cache = _load_cache()
    if not force:
        cached = cache.get("fingerprints", {}).get(repo, "")
        if cached and cached.split("-")[0] == _fingerprint(paths):
            if "-" not in cached:
                return []  # 旧指纹（仅路径），未变
    # 一次 codeload 整仓 zip（1-16s，无重定向坑），本地解压抽 SKILL.md 解析 frontmatter
    zip_bytes = _codeload_zip(repo)
    if not zip_bytes:
        return []
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.lower().endswith("skill.md") or ".claude-plugin/plugin.json" in name.lower():
                    files[name] = zf.read(info)
    except Exception:
        logger.warning("discovery zip parse failed %s", repo, exc_info=True)
        return []
    # 内容指纹：对 zip 内 SKILL.md 内容算 md5（按 skill 相对路径）
    content_hashes: dict[str, str] = {}
    for name, data in files.items():
        # 归一化为技能相对路径（去掉 repo-main/ 前缀根）
        rel = name.split("/", 1)[1] if "/" in name else name
        content_hashes[rel] = hashlib.md5(data).hexdigest()[:12]
    fp = _fingerprint(paths, content_hashes)
    if not force and cache.get("fingerprints", {}).get(repo) == fp:
        return []  # 内容未变，跳过
    entries = []
    for spec in entries_spec:
        # zip 内路径带 repo-<branch>/ 前缀（repo-main/skills/...）
        fname = None
        for n in files:
            if n.endswith(spec["skill_path"].lstrip("/")):
                fname = n
                break
        content = files.get(fname or "")
        if not content:
            continue
        text = content.decode("utf-8", "ignore")
        meta, body = _parse_frontmatter(text)
        skill_name = str(meta.get("name") or spec["skill_path"].rsplit("/", 1)[-2].replace("-", " ").title())
        entries.append({
            "name": _safe_name(str(meta.get("name") or skill_name)),
            "display_name": str(meta.get("name") or skill_name),
            "version": fp[:8],
            "description": str(meta.get("description") or _first_desc_line(body)),
            "author": {"name": repo.split("/")[0]},
            "category": "github-discovered",
            "source_url": f"https://github.com/{repo}/tree/main/{spec['skill_path']}",
            "source_kind": "openclaw-skill",
            "repo": repo,
            "skill_path": spec["skill_path"],
            "permissions": _infer_permissions(meta),
            "sha256": "",
            "external_deps": _has_external_deps(meta),
        })
    # Claude 插件条目叠加（若存在 plugin.json）
    if has_claude:
        entries.extend(_scan_claude_plugin(repo, files))
    cache.setdefault("fingerprints", {})[repo] = fp
    _save_cache(cache)
    return entries


def run_discovery(force: bool = False, max_repos: int = 60) -> dict:
    """执行一轮抓取。返回统计。
    轮换策略：新候选（未扫描/未变更）优先扫，已扫且指纹未变的垫底——打破
    top-N 死局，让不同轮的候选都能进库。"""
    global _CANDIDATES
    import time as _t
    index = _load_index()
    last_ts = index.get("last_scan_ts", 0)
    elapsed = _t.time() - last_ts
    if not force and last_ts and elapsed < 24 * 3600:
        # 增量：优先扫描新面孔（不限于 top-60）
        return _run_incremental(index, max_repos)

    candidates = _collect_candidates()
    # 新面孔（未登记或已登记但指纹已变）优先；已扫稳定的垫底
    seen_old: dict[str, dict] = index.get("repos", {})
    def _priority(item):
        repo, meta = item
        rec = seen_old.get(repo)
        if rec is None:
            return (0, -meta.get("stars", 0))       # 未扫描 → 最优先
        return (1, -meta.get("stars", 0))           # 已扫描 → 次之
    repos = sorted(candidates.items(), key=_priority)[:max_repos]

    scanned = 0
    total_entries = 0
    seen: dict[str, dict] = dict(seen_old)
    entries_map: dict[tuple, dict] = {}
    # 历史条目基线（repo 仍要被保留才沿用）
    for e in index.get("entries", []):
        entries_map[(e.get("repo", ""), e.get("skill_path", ""))] = e

    for repo, meta in repos:
        if _t.time() - last_ts < 8:  # 节流 core 配额（1 请求/仓库）
            time.sleep(0.5)
        try:
            entries = _scan_repo(repo, force=force)
            if entries:
                seen[repo] = {"stars": meta.get("stars", 0), "entries": len(entries),
                              "reason": meta.get("reason", ""), "ts": _t.time(), "scanned": True}
                for e in entries:
                    e["stars"] = meta.get("stars", 0)
                    entries_map[(repo, e["skill_path"])] = e
                total_entries += len(entries)
            else:
                # 无技能 或 未变更：保留 repo 但标记
                seen.setdefault(repo, {"stars": meta.get("stars", 0), "entries": 0,
                                       "reason": meta.get("reason", ""), "ts": _t.time(),
                                       "scanned": True})
            scanned += 1
        except Exception as e:
            logger.warning("scan fail %s: %s", repo, e)
        if scanned >= 10 and scanned % 10 == 0:
            logger.info("discovery progress: %d repos / %d entries", scanned, total_entries)

    # 清理：条目保留前提是其 repo 仍在本轮扫描集（看过树）或历史登记的 repos 里。
    # 这里 seen 已含所有本轮候选，历史 repos 未在本轮 → 保留其条目避免丢积累。
    all_repos = set(seen.keys()) | set(index.get("repos", {}).keys())
    index["entries"] = [e for e in entries_map.values() if e.get("repo") in all_repos]
    index["repos"] = seen
    index["last_scan_ts"] = _t.time()
    index["scan_stats"] = {"scanned": scanned, "entries": len(index["entries"]), "candidates": len(candidates)}
    _save_index(index)
    logger.info("Discovery 扫描完成: %d 仓库 / %d 条目（候选 %d）", scanned, len(index["entries"]), len(candidates))
    return {"status": "ok", "scanned": scanned, "entries": len(index["entries"]), "candidates": len(candidates)}


def _run_incremental(index: dict, max_repos: int) -> dict:
    """24h 内的增量扫描：只扫新面孔（index 未登记的候选），不动已扫仓库。"""
    import time as _t
    candidates = _collect_candidates()
    seen: dict[str, dict] = index.get("repos", {})
    fresh = {repo: meta for repo, meta in candidates.items() if repo not in seen}
    repos = sorted(fresh.items(), key=lambda kv: -kv[1].get("stars", 0))[:max_repos]
    if not repos:
        return {"status": "ok", "skipped": "no-new", "last_scan_ts": index.get("last_scan_ts", 0)}

    entries_map: dict[tuple, dict] = {}
    for e in index.get("entries", []):
        entries_map[(e.get("repo", ""), e.get("skill_path", ""))] = e
    scanned = 0
    total = 0
    for repo, meta in repos:
        try:
            entries = _scan_repo(repo, force=True)  # 新面孔强制扫
            if entries:
                seen[repo] = {"stars": meta.get("stars", 0), "entries": len(entries),
                              "reason": meta.get("reason", ""), "ts": _t.time(), "scanned": True}
                for e in entries:
                    e["stars"] = meta.get("stars", 0)
                    entries_map[(repo, e["skill_path"])] = e
                total += len(entries)
            else:
                seen.setdefault(repo, {"stars": meta.get("stars", 0), "entries": 0,
                                       "reason": meta.get("reason", ""), "ts": _t.time(),
                                       "scanned": True})
            scanned += 1
        except Exception as e:
            logger.warning("incremental scan fail %s: %s", repo, e)
    index["repos"] = seen
    index["entries"] = list(entries_map.values())
    index["last_scan_ts"] = _t.time()
    index["scan_stats"] = {"scanned": scanned, "entries": len(index["entries"]),
                           "candidates": len(candidates), "mode": "incremental"}
    _save_index(index)
    return {"status": "ok", "scanned": scanned, "entries": len(index["entries"]),
            "candidates": len(candidates), "mode": "incremental"}


def discover_status() -> dict:
    index = _load_index()
    return {
        "status": "ok",
        "last_scan_ts": index.get("last_scan_ts", 0),
        "repos": len(index.get("repos", {})),
        "entries": len(index.get("entries", [])),
        "stats": index.get("scan_stats", {}),
        "thread_lock": True,  # 标记可并发调用（不阻塞市场加载）
    }


def get_discovered_entries() -> list[dict]:
    """市场聚合用的发现条目快照（注入仓库星数）。"""
    index = _load_index()
    repo_stars = {repo: rec.get("stars", 0) for repo, rec in index.get("repos", {}).items()}
    out = []
    for e in index.get("entries", []):
        e = dict(e)
        e["stars"] = repo_stars.get(e.get("repo", ""), 0)
        out.append(e)
    return out


# 保证索引目录存在
_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
