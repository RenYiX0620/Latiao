"""Latiao 扩展管理器（Extension Manager）——对齐 ZCode/Claude 插件市场体系。

扩展包（.latiaoext = zip）：
    manifest.yaml          # name/version/description(/_i18n)/author/permissions
    plugin.py              # 可选：工具插件（NAME/DEFINITION/PERMISSION/execute）
    skills/*.md            # 可选：技能（复用 SKILL.md 格式）
    agents/*.md            # 可选：子智能体身份

目录布局（对齐 ZCode）：
    ~/.local-ai-os/extensions/<name>/<version>/   解压后的本体
    ~/.local-ai-os/extensions/.installed.json     已装清单（来源/sha256/启用状态）

安全约束：
    - zip 解压防路径逃逸（归一化后必须位于目标目录内）
    - 解压总量上限 50MB（防 zip 炸弹）
    - 安装时校验 manifest 的 name/version；权限声明分级 read-only/files/network/shell
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import httpx

logger = logging.getLogger("latiao-sidecar")

EXTENSIONS_DIR = Path.home() / ".local-ai-os" / "extensions"
INSTALLED_FILE = EXTENSIONS_DIR / ".installed.json"
MARKET_SOURCES_FILE = Path.home() / ".local-ai-os" / "market_sources.json"

_MAX_EXTRACT_BYTES = 50 * 1024 * 1024  # 50MB 上限，防 zip 炸弹

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$", re.IGNORECASE)
_VERSION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,31}$")

_VALID_PERMISSIONS = {"readonly", "files", "network", "shell"}

_GITHUB_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+)/(.+))?$",
    re.IGNORECASE,
)


def _load_installed() -> dict:
    try:
        if INSTALLED_FILE.exists():
            data = json.loads(INSTALLED_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("extensions"), list):
                return data
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to load installed extensions state", exc_info=True)
    return {"extensions": []}


def _save_installed(state: dict):
    EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INSTALLED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INSTALLED_FILE)


def _find_record(state: dict, name: str) -> dict | None:
    for rec in state["extensions"]:
        if rec.get("name") == name:
            return rec
    return None


def list_extensions() -> list[dict]:
    """已装扩展列表（含 enabled 状态）。"""
    state = _load_installed()
    out = []
    for rec in state["extensions"]:
        name = rec.get("name", "")
        ver = rec.get("version", "")
        pkg_dir = EXTENSIONS_DIR / name / ver
        manifest = _read_manifest(pkg_dir) or {}
        out.append({
            "name": name,
            "version": ver,
            "description": manifest.get("description", rec.get("description", "")),
            "description_i18n": manifest.get("description_i18n", {}),
            "author": manifest.get("author", rec.get("author", {})),
            "permissions": manifest.get("permissions", []),
            "enabled": bool(rec.get("enabled", True)),
            "source": rec.get("source", ""),
            "installed_at": rec.get("installed_at", 0),
            "has_plugin": (pkg_dir / "plugin.py").exists(),
            "has_skills": (pkg_dir / "skills").is_dir(),
            "has_agents": (pkg_dir / "agents").is_dir(),
        })
    return out


def active_extension_dirs() -> list[Path]:
    """启用中的扩展包目录（供插件/技能加载侧扫描）。"""
    state = _load_installed()
    out = []
    for rec in state["extensions"]:
        if not rec.get("enabled", True):
            continue
        d = EXTENSIONS_DIR / rec.get("name", "") / rec.get("version", "")
        if d.is_dir():
            out.append(d)
    return out


def _read_manifest(pkg_dir: Path) -> dict | None:
    mf = pkg_dir / "manifest.yaml"
    if not mf.exists():
        mf = pkg_dir / "manifest.yml"
    if not mf.exists():
        return None
    try:
        import yaml
        data = yaml.load(mf.read_text(encoding="utf-8"), Loader=yaml.SafeLoader)
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("Failed to parse manifest in %s", pkg_dir, exc_info=True)
        return None


def _safe_extract(zip_bytes: bytes, dest: Path) -> list[str]:
    """安全解压：防路径逃逸 + 总量上限。返回解压出的文件名列表。"""
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            # 路径归一化防逃逸（../、绝对路径、盘符）
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[a-zA-Z]:", name):
                raise ValueError(f"非法路径: {info.filename}")
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest.resolve()) + "/") and target != dest.resolve():
                raise ValueError(f"路径逃逸: {info.filename}")
            total += info.file_size
            if total > _MAX_EXTRACT_BYTES:
                raise ValueError(f"扩展包解压后超过 {_MAX_EXTRACT_BYTES // (1024*1024)}MB 上限")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written.append(name)
    return written


def _download(url: str) -> bytes:
    """下载 zip（url 或 github repo）。返回字节。"""
    m = _GITHUB_RE.match(url.strip())
    if m:
        owner, repo, ref, subdir = m.group(1), m.group(2), m.group(3), m.group(4)
        if ref:
            url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}"
        else:
            url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/main"
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except Exception:
            # 镜像兜底：raw.githubusercontent -> jsDelivr
            import re as _re
            _m = _re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/main/(.+)$", url)
            murl = ("https://cdn.jsdelivr.net/gh/%s/%s@main/%s" % _m.groups()) if _m else ""
            if not murl:
                raise
            resp = client.get(murl)
            resp.raise_for_status()
        data = resp.content
    # GitHub codeload zip 是仓库整体包：若指定了子目录，需要裁剪
    if m and subdir:
        data = _subdir_zip(data, subdir)
    return data


def _subdir_zip(zip_bytes: bytes, subdir: str) -> bytes:
    """从仓库 zip 中裁剪出子目录，重打包。"""
    prefix = subdir.strip("/") + "/"
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            name = info.filename
            if name.startswith(prefix) and len(name) > len(prefix):
                new_name = name[len(prefix):]
                zout.writestr(new_name, zin.read(info))
    return out.getvalue()


def install_extension(source: str, sha256: str = "", label: str = "") -> dict:
    """安装扩展：本地路径 / URL / GitHub repo。返回 {status, name, version, permissions}。"""
    source = (source or "").strip()
    if not source:
        return {"status": "error", "message": "扩展来源不能为空"}
    try:
        if Path(source).expanduser().is_file():
            zip_bytes = Path(source).expanduser().read_bytes()
            src_desc = f"file:{source}"
        else:
            zip_bytes = _download(source)
            src_desc = source
    except Exception as e:
        return {"status": "error", "message": f"下载失败: {e}"}

    # sha256 校验
    if sha256:
        actual = hashlib.sha256(zip_bytes).hexdigest()
        if actual.lower() != sha256.lower():
            return {"status": "error", "message": f"sha256 校验失败（期望 {sha256[:12]}…）"}
    digest = hashlib.sha256(zip_bytes).hexdigest()

    # 先解压到临时目录读 manifest，再定稿
    with tempfile.TemporaryDirectory() as tmp:
        try:
            _safe_extract(zip_bytes, Path(tmp))
        except ValueError as e:
            return {"status": "error", "message": f"包校验失败: {e}"}
        # 包根定位：支持 zip -r 产生的单层目录包装（finance-pack/manifest.yaml）
        pkg_root = Path(tmp)
        manifest = _read_manifest(pkg_root)
        if not manifest and pkg_root.is_dir():
            child_dirs = [d for d in pkg_root.iterdir() if d.is_dir()]
            for d in child_dirs:
                mf = _read_manifest(d)
                if mf:
                    pkg_root = d
                    manifest = mf
                    break
        if not manifest:
            return {"status": "error", "message": "扩展包缺少 manifest.yaml"}
        name = str(manifest.get("name", "")).strip()
        version = str(manifest.get("version", "")).strip()
        if not _NAME_RE.match(name):
            return {"status": "error", "message": f"manifest name 非法: {name!r}"}
        if not _VERSION_RE.match(version):
            return {"status": "error", "message": f"manifest version 非法: {version!r}"}
        perms = manifest.get("permissions") or []
        if not isinstance(perms, list) or any(p not in _VALID_PERMISSIONS for p in perms):
            return {"status": "error", "message": f"manifest permissions 非法: {perms!r}"}
        # 无 permissions 声明时默认只读
        if not perms:
            perms = ["readonly"]
        has_content = any(
            (pkg_root / f).exists() for f in ("plugin.py", "skills", "agents")
        )
        if not has_content:
            return {"status": "error", "message": "扩展包没有任何内容（plugin.py/skills/agents）"}

        state = _load_installed()
        existing = _find_record(state, name)
        if existing and existing.get("version") == version:
            return {"status": "error", "message": f"扩展 {name}@{version} 已安装"}

        pkg_dir = EXTENSIONS_DIR / name / version
        if pkg_dir.exists():
            shutil.rmtree(pkg_dir)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        for item in pkg_root.iterdir():
            if item.is_dir():
                shutil.copytree(item, pkg_dir / item.name)
            else:
                shutil.copy2(item, pkg_dir / item.name)

        record = {
            "name": name,
            "version": version,
            "source": src_desc,
            "sha256": digest,
            "label": label,
            "enabled": True,
            "installed_at": __import__("time").time(),
        }
        if existing:
            # 升级：替换记录
            for i, r in enumerate(state["extensions"]):
                if r.get("name") == name:
                    state["extensions"][i] = record
                    break
        else:
            state["extensions"].append(record)
        _save_installed(state)
        logger.info("扩展已安装: %s@%s (%s)", name, version, src_desc)
        return {
            "status": "ok", "name": name, "version": version,
            "permissions": perms,
            "message": f"已安装 {name}@{version}",
        }


# ── 市场（Phase 2a）──
# jsDelivr CDN 国内可达；raw.githubusercontent 常被屏蔽。fetch 内自动 fallback。
DEFAULT_MARKETPLACE = "https://cdn.jsdelivr.net/gh/RenYiX0620/latiao-marketplace@main/marketplace.json"
# 扩展 zip 下载同理：source.url 是 jsDelivr 链接，raw 版本兜底
MIRROR_SUFFIXES = (
    ("cdn.jsdelivr.net/gh/", "raw.githubusercontent.com/"),
)
_ZIP_URL_RE = __import__("re").compile(r"cdn\.jsdelivr\.net/gh/([^/]+)/([^/]+)@main/(.+)$")


def fetch_marketplace(url: str = "", timeout: float = 20) -> dict:
    """拉取 marketplace.json：返回规范化插件列表。
    支持 http(s) URL 或本地文件路径（开发用）。"""
    url = (url or DEFAULT_MARKETPLACE).strip()
    try:
        if url.startswith(("http://", "https://")):
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception:
                    # 镜像兜底：jsDelivr -> raw.githubusercontent
                    _m = _ZIP_URL_RE.match(url)
                    rurl = "https://raw.githubusercontent.com/%s/%s/main/%s" % _m.groups() if _m else ""
                    if not rurl:
                        raise
                    resp = client.get(rurl)
                    resp.raise_for_status()
                    data = resp.json()
        else:
            p = Path(url).expanduser()
            if not p.exists():
                return {"status": "error", "message": f"市场文件不存在: {url}"}
            import yaml as _yaml
            text = p.read_text(encoding="utf-8")
            data = _yaml.safe_load(text) if p.suffix in (".yaml", ".yml") else json.loads(text)
    except Exception as e:
        return {"status": "error", "message": f"拉取市场失败: {e}"}

    plugins = []
    for p_ in data.get("plugins", []) or []:
        source = p_.get("source") or {}
        plugins.append({
            "name": p_.get("name", ""),
            "version": p_.get("version", "0.0.0"),
            "description": p_.get("description", ""),
            "description_i18n": p_.get("description_i18n", {}),
            "author": p_.get("author", {}),
            "category": p_.get("category", ""),
            "keywords": p_.get("keywords", []),
            "source_url": source.get("url", ""),
            "sha256": source.get("sha256", ""),
        })
    out = {"status": "ok", "name": data.get("name", ""),
           "description": data.get("description", ""), "plugins": plugins}
    installed = _load_installed()
    for p_ in out["plugins"]:
        rec = _find_record(installed, p_["name"])
        if rec:
            p_["installed"] = True
            p_["installed_version"] = rec.get("version", "")
            p_["update_available"] = rec.get("version", "") != p_["version"]
        else:
            p_["installed"] = False
            p_["update_available"] = False
    return out


# ── 市场缓存（启动预热 + 5 分钟 TTL）──
_MARKET_CACHE: dict = {"": {"ts": 0.0, "data": None}}
_MARKET_FETCHING = False


def get_marketplace_cached(url: str = "") -> dict:
    """带缓存的 market 读取：命中 TTL 内缓存直接返回；miss 时同步拉取。"""
    import time as _t
    global _MARKET_FETCHING
    key = (url or DEFAULT_MARKETPLACE).strip()
    entry = _MARKET_CACHE.get(key)
    if entry and entry["data"] and _t.time() - entry["ts"] < 300:
        return entry["data"]
    data = fetch_marketplace(url)
    if data.get("status") == "ok":
        _MARKET_CACHE[key] = {"ts": _t.time(), "data": data}
    return data


def warm_market_cache() -> None:
    """后台预热官方市场（sidecar 启动时调用，不阻塞启动）。"""
    import threading
    def _worker():
        try:
            data = fetch_marketplace("")
            if data.get("status") == "ok":
                import time as _t
                _MARKET_CACHE[DEFAULT_MARKETPLACE] = {"ts": _t.time(), "data": data}
                logger.info("市场预热完成: %d 个扩展", len(data.get("plugins", [])))
        except Exception:
            logger.warning("市场预热失败", exc_info=True)
    threading.Thread(target=_worker, daemon=True).start()


def uninstall_extension(name: str) -> dict:
    state = _load_installed()
    rec = _find_record(state, name)
    if not rec:
        return {"status": "error", "message": f"扩展 {name} 未安装"}
    state["extensions"] = [r for r in state["extensions"] if r.get("name") != name]
    _save_installed(state)
    pkg = EXTENSIONS_DIR / name
    if pkg.exists():
        shutil.rmtree(pkg, ignore_errors=True)
    logger.info("扩展已卸载: %s", name)
    return {"status": "ok", "message": f"已卸载 {name}"}


def set_extension_enabled(name: str, enabled: bool) -> dict:
    state = _load_installed()
    rec = _find_record(state, name)
    if not rec:
        return {"status": "error", "message": f"扩展 {name} 未安装"}
    rec["enabled"] = enabled
    _save_installed(state)
    logger.info("扩展 %s 已%s", name, "启用" if enabled else "禁用")
    return {"status": "ok", "message": f"{name} 已{'启用' if enabled else '禁用'}"}


# ═══════════════════════════════════════════════════════
#  多市场源（Phase 1）：官方 + 用户自定义 + 生态仓库发现源
# ═══════════════════════════════════════════════════════

DEFAULT_SOURCES = [
    {
        "id": "official",
        "name": "Latiao 官方",
        "description": "官方扩展市场：工具/技能/子智能体组合包",
        "url": DEFAULT_MARKETPLACE,
        "kind": "marketplace",
        "builtin": True,
    },
    {
        "id": "openclaw-skills",
        "name": "OpenClaw 技能库",
        "description": "社区技能仓库（SKILL.md 格式，发现式浏览）",
        "url": "https://github.com/21-DOT-DEV/openclaw-skills",
        "kind": "openclaw",
        "builtin": True,
    },
]


def _load_sources() -> dict:
    try:
        if MARKET_SOURCES_FILE.exists():
            data = json.loads(MARKET_SOURCES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sources"), list):
                return data
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to load market sources", exc_info=True)
    return {"sources": []}


def _save_sources(state: dict):
    PROGRESS_DIR = Path.home() / ".local-ai-os"
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MARKET_SOURCES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(MARKET_SOURCES_FILE)


def list_market_sources() -> list[dict]:
    """市场源列表：内置 + 用户添加。"""
    state = _load_sources()
    user_sources = state.get("sources", [])
    builtin = [s for s in DEFAULT_SOURCES]
    user_keys = {s.get("url") for s in user_sources}
    for b in builtin:
        if b["url"] in user_keys:
            b["removed"] = True
    return builtin + user_sources


def add_market_source(url: str, name: str = "", kind: str = "") -> dict:
    """添加市场源。url：marketplace.json URL 或 github 仓库地址（生态源自动识别）。"""
    from adapters import parse_github_repo
    url = (url or "").strip()
    if not url:
        return {"status": "error", "message": "url 不能为空"}
    repo = parse_github_repo(url)
    if repo and not kind:
        kind = "openclaw"  # 会被 discover 自动细判，先按生态源
    if not kind:
        kind = "marketplace"
    if not name:
        name = repo or url.split("/")[-1].replace(".json", "")
    state = _load_sources()
    for s in state["sources"]:
        if s.get("url") == url:
            return {"status": "ok", "message": "源已存在", "source": s}
    src = {"id": f"user-{len(state['sources'])+1}", "name": name, "description": "",
           "url": url, "kind": kind, "builtin": False}
    state["sources"].append(src)
    _save_sources(state)
    return {"status": "ok", "message": "已添加市场源", "source": src}


def remove_market_source(url: str) -> dict:
    state = _load_sources()
    before = len(state["sources"])
    state["sources"] = [s for s in state["sources"] if s.get("url") != url]
    if len(state["sources"]) == before:
        return {"status": "error", "message": "源不存在或不可删除（内置源）"}
    _save_sources(state)
    return {"status": "ok", "message": "已移除市场源"}


def fetch_all_markets() -> dict:
    """聚合所有源的条目（官方 marketplace + 生态源发现）。生态源走 adapters。"""
    sources = list_market_sources()
    merged = []
    errors = []
    for src in sources:
        if src.get("removed"):
            continue
        try:
            if src.get("kind") == "marketplace" or src["url"].endswith((".json", ".yaml", ".yml")):
                data = fetch_marketplace(src["url"])
                if data.get("status") == "ok":
                    for p in data.get("plugins", []):
                        p["market_source"] = src["name"]
                        merged.append(p)
                else:
                    errors.append(f"{src['name']}: {data.get('message', '拉取失败')}")
            else:
                # 生态源：jsDelivr 树发现（openclaw / generic）
                from adapters import discover_auto
                data = discover_auto(src["url"])
                if data.get("status") == "ok":
                    for p in data.get("plugins", []):
                        p["market_source"] = src["name"]
                        merged.append(p)
                else:
                    errors.append(f"{src['name']}: {data.get('message', '发现失败')}")
        except Exception as e:
            logger.warning("fetch source %s failed", src.get("url"), exc_info=True)
            errors.append(f"{src['name']}: {e}")
    # GitHub 自动发现索引并入（主动抓取的结果，source_kind=openclaw-skill，带 repo/skill_path）
    try:
        from discovery import get_discovered_entries
        for p in get_discovered_entries():
            p = dict(p)
            p["market_source"] = "GitHub 发现"
            merged.append(p)
    except Exception:
        logger.warning("discovery entries merge failed", exc_info=True)
    return {"status": "ok", "plugins": merged, "errors": errors, "count": len(merged)}


def install_github_item(repo: str, skill_path: str = "", kind: str = "openclaw-skill") -> dict:
    """安装生态源条目：下载/打包成 .latiaoext 走 install_extension。"""
    from adapters import (install_openclaw_skill, install_claude_plugin,
                          parse_github_repo)
    repo = parse_github_repo(repo)
    if not repo:
        return {"status": "error", "message": f"无法识别仓库: {repo!r}"}
    if kind.startswith("openclaw") and skill_path:
        zip_bytes = install_openclaw_skill(repo, skill_path)
    elif kind.startswith("claude"):
        zip_bytes = install_claude_plugin(repo)
    else:
        zip_bytes = install_openclaw_skill(repo, skill_path) if skill_path else install_claude_plugin(repo)
    if zip_bytes is None:
        return {"status": "error", "message": "下载/打包失败（源不可达或格式不符）"}
    import tempfile as _tf
    label = skill_path or f"{repo}"
    with _tf.TemporaryDirectory() as tmp:
        p = Path(tmp) / "pkg.latiaoext"
        p.write_bytes(zip_bytes)
        result = install_extension(str(p), "", label=label)
        return result


# ═══════════════════════════════════════════════════════
#  多市场源（Phase 1）：官方 + 用户自定义 + 生态仓库发现源
# ═══════════════════════════════════════════════════════

DEFAULT_SOURCES = [
    {
        "id": "official",
        "name": "Latiao 官方",
        "description": "官方扩展市场：工具/技能/子智能体组合包",
        "url": DEFAULT_MARKETPLACE,
        "kind": "marketplace",
        "builtin": True,
    },
    {
        "id": "openclaw-skills",
        "name": "OpenClaw 技能库",
        "description": "社区技能仓库（SKILL.md 格式，发现式浏览）",
        "url": "https://github.com/21-DOT-DEV/openclaw-skills",
        "kind": "openclaw",
        "builtin": True,
    },
]
