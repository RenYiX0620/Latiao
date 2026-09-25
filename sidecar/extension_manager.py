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
import os
import re
import threading
import time
import uuid
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
    with httpx.Client(timeout=15, follow_redirects=True) as client:
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


def _is_local_source(source: str) -> bool:
    """本地文件 = 管理员显式操作；其余视为网络来源。只看形态，不下载。"""
    try:
        return Path(source).expanduser().is_file()
    except OSError:
        return False


def _source_policy_error(source: str) -> dict | None:
    """网络来源的准入检查（**必须在下载之前**）：封锁名单 + 摘要必填。

    原先这两项写在 _load_source_bytes 之后——被封锁的来源也照样先下载了一遍，
    与注释声明的"在任何下载动作之前"相反（2026-09-23 实测暴露）。
    """
    if _is_local_source(source):
        return None
    if is_source_blocked(source):
        logger.warning("install blocked by policy: %s", source)
        return {"status": "error",
                "message": f"该来源已被封锁，拒绝安装：{source}（可在扩展页解封后重试）"}
    return None


def _load_source_bytes(source: str) -> tuple[bytes | None, str, str | None]:
    """取回扩展包字节。返回 (zip_bytes, src_desc, error)。"""
    try:
        if Path(source).expanduser().is_file():
            return Path(source).expanduser().read_bytes(), f"file:{source}", None
        return _download(source), source, None
    except Exception as e:
        return None, "", f"下载失败: {e}"


def _extract_and_validate(zip_bytes: bytes, tmp: Path) -> tuple[Path | None, dict, dict | None]:
    """安全解压 + manifest 定位校验 + 内容检查 → (pkg_root, manifest, error)。

    预览（stage）与落盘（install）共用这一份判定，避免"预览放行、安装又拒绝"漂移。
    """
    try:
        _safe_extract(zip_bytes, tmp)
    except ValueError as e:
        return None, {}, {"status": "error", "message": f"包校验失败: {e}"}
    # 包根定位：支持 zip -r 产生的单层目录包装（finance-pack/manifest.yaml）
    pkg_root = tmp
    manifest = _read_manifest(pkg_root)
    if not manifest and pkg_root.is_dir():
        for d in [d for d in pkg_root.iterdir() if d.is_dir()]:
            mf = _read_manifest(d)
            if mf:
                pkg_root, manifest = d, mf
                break
    if not manifest:
        return None, {}, {"status": "error", "message": "扩展包缺少 manifest.yaml"}
    name = str(manifest.get("name", "")).strip()
    version = str(manifest.get("version", "")).strip()
    if not _NAME_RE.match(name):
        return None, {}, {"status": "error", "message": f"manifest name 非法: {name!r}"}
    if not _VERSION_RE.match(version):
        return None, {}, {"status": "error", "message": f"manifest version 非法: {version!r}"}
    perms = manifest.get("permissions") or []
    if not isinstance(perms, list) or any(p not in _VALID_PERMISSIONS for p in perms):
        return None, {}, {"status": "error", "message": f"manifest permissions 非法: {perms!r}"}
    if not perms:
        perms = ["readonly"]                      # 无声明 → 默认只读
    has_content = any((pkg_root / f).exists() for f in ("plugin.py", "skills", "agents"))
    if not has_content:
        return None, {}, {"status": "error", "message": "扩展包没有任何内容（plugin.py/skills/agents）"}
    # 安装前 AST 静态审查（P0）：plugin.py 落盘后会被 tool_system.exec_module 加载执行。
    # 只拦**安全类**问题（禁调用/语法错误）；NAME/DEFINITION/PERMISSION 缺失是风格约定。
    plugin_py = pkg_root / "plugin.py"
    if plugin_py.exists():
        try:
            from plugin_creator import validate_plugin_code
            v = validate_plugin_code(plugin_py.read_text("utf-8", errors="ignore"))
            sec_errors = [e for e in (v.get("errors") or [])
                          if ("禁止的调用" in e) or ("语法错误" in e)]
            if sec_errors:
                return None, {}, {
                    "status": "error",
                    "message": "扩展包 plugin.py 未通过静态安全审查: " + "; ".join(sec_errors),
                }
        except Exception as e:
            return None, {}, {"status": "error", "message": f"扩展包代码审查失败: {e}"}
    return pkg_root, {**manifest, "name": name, "version": version, "permissions": perms}, None


def _package_preview(zip_bytes: bytes) -> tuple[dict | None, dict | None]:
    """只读预检 → (preview, error)。不落任何文件、不改任何状态。"""
    digest = hashlib.sha256(zip_bytes).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        pkg_root, manifest, err = _extract_and_validate(zip_bytes, Path(tmp))
        if err:
            return None, err
        files = sorted(str(q.relative_to(pkg_root)) for q in pkg_root.rglob("*") if q.is_file())
        return {
            "name": str(manifest.get("name", "")),
            "version": str(manifest.get("version", "")),
            "permissions": list(manifest.get("permissions") or ["readonly"]),
            "files": files[:200],
            "file_count": len(files),
            "digest": digest,
            "size": len(zip_bytes),
        }, None


def _install_bytes(zip_bytes: bytes, src_desc: str, digest: str, label: str = "") -> dict:
    """把已校验、已确认的字节落到 extensions/ 并记账（install 与 confirm 共用）。"""
    with tempfile.TemporaryDirectory() as tmp:
        pkg_root, manifest, err = _extract_and_validate(zip_bytes, Path(tmp))
        if err:
            return err
        name = str(manifest["name"])
        version = str(manifest["version"])
        perms = list(manifest["permissions"])

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
        logger.info("扩展已安装: %s@%s (%s, sha256=%s…)", name, version, src_desc, digest[:12])
        return {
            "status": "ok", "name": name, "version": version,
            "permissions": perms, "sha256": digest,
            "message": f"已安装 {name}@{version}",
        }


def install_extension(source: str, sha256: str = "", label: str = "",
                      confirmed: bool = False) -> dict:
    """安装扩展：本地路径 / URL / GitHub repo。返回 {status, name, version, permissions}。

    ⑦ 服务端确认闸门（审计 2026-09-23）：扩展代码会在下次启动/热重载时执行，
    安装因此**必须**带 confirmed=True，且只应由"用户已在界面上确认过"的调用方
    设置（API 的 confirm 端点、confirm 级的 create_skill 工具）。默认 False 意味着
    将来新增的调用点若忘了走确认流，会被明确拒绝而不是静默装进去。
    """
    source = (source or "").strip()
    if not source:
        return {"status": "error", "message": "扩展来源不能为空"}
    if not confirmed:
        return {"status": "error",
                "message": "⛔ 安装扩展需要用户确认（扩展代码会在下次启动/热重载时执行）："
                           "先调 /v1/extensions/install 取预览，用户确认后再调 "
                           "/v1/extensions/install/confirm（带 pending_id 与 sha256）"}
    # 准入检查先于下载：封锁来源不接触、无摘要的网络来源不下载
    policy_err = _source_policy_error(source)
    if policy_err:
        return policy_err
    # sha256 校验（审计 P1-10）：网络来源必须提供 sha256——扩展无签名体系，
    # 传输完整性 hash 是仅有的防线；本地文件视为管理员显式操作，允许免传。
    if not _is_local_source(source) and not sha256:
        return {"status": "error",
                "message": "网络来源安装必须提供 sha256（防传输篡改）；请从可信市场清单或发布页获取后重试"}
    zip_bytes, src_desc, err = _load_source_bytes(source)
    if err:
        return {"status": "error", "message": err}
    digest = hashlib.sha256(zip_bytes).hexdigest()
    if sha256 and digest.lower() != sha256.lower():
        return {"status": "error", "message": f"sha256 校验失败（期望 {sha256[:12]}…）"}
    return _install_bytes(zip_bytes, src_desc, digest, label)


# ── ⑦ 分阶段安装：stage → 用户确认 → confirm（⑥ 摘要绑定）──────────────
# 为什么两阶段：扩展是"下次启动就执行的代码"，安装不该是某个请求的副作用。
# 服务端先取回内容、算摘要、给出预览（文件清单/权限/摘要），只有携带**预览里
# 那个摘要**的确认请求才会落盘——预览与安装之间内容若变了（URL 内容可变、仓库
# 被改），摘要对不上就装不进去。⑥ 的"sha256 必填"落在同一处：确认必须回传非空
# 摘要，空摘要直接拒。
_PENDING_LOCK = threading.Lock()
_PENDING: dict[str, dict] = {}
_PENDING_TTL = 900.0      # 15 分钟未确认即作废
_PENDING_MAX = 8
_PENDING_DIR = Path(tempfile.gettempdir()) / "latiao-pending-install"


def _pending_gc() -> None:
    now = time.time()
    for pid, rec in list(_PENDING.items()):
        if now - rec["created"] > _PENDING_TTL:
            _PENDING.pop(pid, None)
            try:
                rec["path"].unlink()
            except OSError:
                pass


def stage_package(zip_bytes: bytes, src_desc: str, expect_sha256: str = "",
                  label: str = "") -> dict:
    """第一阶段：校验 + 算摘要 + 落待确认区（**不安装**）→ 返回预览。"""
    preview, err = _package_preview(zip_bytes)
    if err:
        return err
    if expect_sha256 and preview["digest"].lower() != expect_sha256.lower():
        return {"status": "error",
                "message": f"sha256 校验失败（期望 {expect_sha256[:12]}…，"
                           f"实际 {preview['digest'][:12]}…）"}
    _PENDING_DIR.mkdir(parents=True, exist_ok=True)
    pending_id = uuid.uuid4().hex
    p = _PENDING_DIR / f"{pending_id}.latiaoext"
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(zip_bytes)
    with _PENDING_LOCK:
        _pending_gc()
        if len(_PENDING) >= _PENDING_MAX:
            oldest = min(_PENDING, key=lambda k: _PENDING[k]["created"])
            gone = _PENDING.pop(oldest, None)
            if gone:
                try:
                    gone["path"].unlink()
                except OSError:
                    pass
        _PENDING[pending_id] = {"path": p, "digest": preview["digest"],
                                "src_desc": src_desc, "label": label,
                                "created": time.time()}
    return {"status": "pending", "pending_id": pending_id, "preview": preview,
            "message": f"待确认安装 {preview['name']}@{preview['version']}（确认前不写入磁盘）"}


def stage_install(source: str, sha256: str = "", label: str = "") -> dict:
    """第一阶段（路径/URL 形态）：取回 → 校验 → 返回预览，不安装。"""
    source = (source or "").strip()
    if not source:
        return {"status": "error", "message": "扩展来源不能为空"}
    policy_err = _source_policy_error(source)
    if policy_err:
        return policy_err
    if not _is_local_source(source) and not sha256:
        return {"status": "error",
                "message": "网络来源安装必须提供 sha256（防传输篡改）；请从可信市场清单或发布页获取后重试"}
    zip_bytes, src_desc, err = _load_source_bytes(source)
    if err:
        return {"status": "error", "message": err}
    return stage_package(zip_bytes, src_desc, expect_sha256=sha256, label=label)


def confirm_install(pending_id: str, digest: str, label: str = "") -> dict:
    """第二阶段：必须回传预览里的 sha256（⑥），一致才真正安装（⑦）。"""
    pending_id = (pending_id or "").strip()
    digest = (digest or "").strip()
    if not pending_id or not digest:
        return {"status": "error",
                "message": "确认安装需要 pending_id 与 sha256（摘要必须回传：防预览之后内容被替换）"}
    with _PENDING_LOCK:
        _pending_gc()
        rec = _PENDING.get(pending_id)
    if not rec:
        return {"status": "error", "message": "待确认安装不存在或已过期，请重新发起安装"}
    try:
        zip_bytes = rec["path"].read_bytes()
    except OSError:
        return {"status": "error", "message": "待确认包已失效，请重新发起安装"}
    actual = hashlib.sha256(zip_bytes).hexdigest()
    if actual.lower() != digest.lower() or actual.lower() != rec["digest"].lower():
        logger.warning("confirm install digest mismatch: pending=%s 确认=%s 实际=%s",
                       pending_id[:8], digest[:12], actual[:12])
        return {"status": "error",
                "message": f"sha256 不匹配（确认的摘要与待安装内容不一致，可能已被替换）："
                           f"实际 {actual[:12]}…"}
    result = _install_bytes(zip_bytes, rec["src_desc"], actual, label or rec["label"])
    if result.get("status") == "ok":
        with _PENDING_LOCK:
            _PENDING.pop(pending_id, None)
        try:
            rec["path"].unlink()
        except OSError:
            pass
    return result


# ── 市场（Phase 2a）──
# jsDelivr CDN 国内可达；raw.githubusercontent 常被屏蔽。fetch 内自动 fallback。
DEFAULT_MARKETPLACE = "https://cdn.jsdelivr.net/gh/RenYiX0620/latiao-marketplace@main/marketplace.json"
# 扩展 zip 下载同理：source.url 是 jsDelivr 链接，raw 版本兜底
MIRROR_SUFFIXES = (
    ("cdn.jsdelivr.net/gh/", "raw.githubusercontent.com/"),
)
_ZIP_URL_RE = __import__("re").compile(r"cdn\.jsdelivr\.net/gh/([^/]+)/([^/]+)@main/(.+)$")


def fetch_marketplace(url: str = "", timeout: float = 6) -> dict:
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


def blocked_sources() -> list[str]:
    """被封锁的来源（repo 或 URL，统一小写存储）。"""
    st = _load_sources()
    b = st.get("blocked")
    return [str(x).lower() for x in b] if isinstance(b, list) else []


def set_source_blocked(source: str, blocked: bool = True) -> dict:
    """封锁/解封来源（最小治理：安装前拦截，不做策略分级）。"""
    from adapters import parse_github_repo
    src = (source or "").strip()
    if not src:
        return {"status": "error", "message": "source 不能为空"}
    key = (parse_github_repo(src) or src).lower()
    st = _load_sources()
    cur = st.get("blocked")
    cur = [str(x) for x in cur] if isinstance(cur, list) else []
    low = [x.lower() for x in cur]
    if blocked and key not in low:
        cur.append(key)
    elif not blocked:
        cur = [x for x in cur if x.lower() != key]
    st["blocked"] = cur
    _save_sources(st)
    return {"status": "ok", "blocked": cur, "message":
            f"{'已封锁' if blocked else '已解封'}：{key}"}


def is_source_blocked(source: str) -> bool:
    """安装前检查：该来源是否被封锁（repo 名或 URL 任一匹配即算）。"""
    from adapters import parse_github_repo
    src = (source or "").strip()
    if not src:
        return False
    low = src.lower()
    repo = (parse_github_repo(src) or "").lower()
    for b in blocked_sources():
        if b == low or (repo and b == repo):
            return True
    return False


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


# 源抓取结果缓存：生态源（jsDelivr 树发现）实测单个要 **55 秒**，而市场页每次打开/
# 刷新都会调本函数 —— 不缓存就等于每次让用户等一分钟（前端请求先超时 → 显示"加载失败"）。
_SOURCE_CACHE: dict[str, tuple[float, dict]] = {}
_SOURCE_CACHE_TTL = 600.0        # 10 分钟；主动刷新仍会走真抓取


def _cached_source_fetch(url: str, fetcher) -> dict:
    now = time.monotonic()
    hit = _SOURCE_CACHE.get(url)
    if hit and now - hit[0] < _SOURCE_CACHE_TTL:
        return hit[1]
    data = fetcher()
    _SOURCE_CACHE[url] = (now, data)
    return data


def fetch_all_markets() -> dict:
    """聚合所有源的条目（官方 marketplace + 生态源发现 + GitHub 自动发现索引）。
    去重：统一 key = (repo, skill_path) 生态条目 / (name, version) marketplace，
    首次出现者赢——避免同一技能被"手动源"+"GitHub 发现"重复展示。
    已安装回填：生态条目按 name 匹配 .installed.json（name 相同视为已装）。"""
    sources = list_market_sources()
    merged: list[dict] = []
    seen_keys: set[tuple] = set()
    errors = []
    installed_records = {}
    try:
        state = _load_installed()
        for rec in state.get("extensions", []):
            installed_records[rec.get("name", "")] = rec
    except Exception:
        pass

    def _append(p: dict):
        # 统一 key
        key: tuple
        if p.get("repo") and p.get("skill_path"):
            key = ("eco", p["repo"].lower(), p["skill_path"].strip().lower())
        else:
            key = ("mkt", p.get("name", ""), p.get("version", ""))
        if key in seen_keys:
            return  # 重复，跳过
        seen_keys.add(key)
        # 安装状态回填（生态条目无法按 exact name 定位时保守显示未装）
        p["installed"] = bool(installed_records.get(str(p.get("name", ""))))
        p["update_available"] = False
        merged.append(p)

    # 源是**串行**抓的：网络不通/被墙时每个源都要耗满自己的超时（20~60s），
    # 三个源叠加就远超前端的请求超时（实测 25s 仍未返回 → 市场页报"加载失败"）。
    # 给整个聚合一个总时限：到点就带着**已拿到的**结果返回，并如实标注哪些源没来得及。
    _deadline = time.monotonic() + 8.0
    _skipped: list[str] = []
    for src in sources:
        if src.get("removed"):
            continue
        if time.monotonic() > _deadline:
            _skipped.append(str(src.get("name") or src.get("url") or "?"))
            continue
        try:
            if src.get("kind") == "marketplace" or src["url"].endswith((".json", ".yaml", ".yml")):
                data = _cached_source_fetch(src["url"], lambda u=src["url"]: fetch_marketplace(u))
                if data.get("status") == "ok":
                    for p in data.get("plugins", []):
                        p["market_source"] = src["name"]
                        _append(p)
                else:
                    errors.append(f"{src['name']}: {data.get('message', '拉取失败')}")
            else:
                # 生态源：jsDelivr 树发现（openclaw / generic）
                from adapters import discover_auto
                data = _cached_source_fetch(src["url"], lambda u=src["url"]: discover_auto(u))
                if data.get("status") == "ok":
                    for p in data.get("plugins", []):
                        p["market_source"] = src["name"]
                        _append(p)
                else:
                    errors.append(f"{src['name']}: {data.get('message', '发现失败')}")
        except Exception as e:
            logger.warning("fetch source %s failed", src.get("url"), exc_info=True)
            errors.append(f"{src['name']}: {e}")
    if _skipped:
        errors.append("未在时限内抓取（网络慢或被墙）：" + "、".join(_skipped))
    # GitHub 自动发现索引并入（主动抓取的结果，source_kind=openclaw-skill，带 repo/skill_path）
    try:
        from discovery import get_discovered_entries
        for p in get_discovered_entries():
            p = dict(p)
            p["market_source"] = "GitHub 发现"
            _append(p)
    except Exception:
        logger.warning("discovery entries merge failed", exc_info=True)
    return {"status": "ok", "plugins": merged, "errors": errors, "count": len(merged)}


def install_github_item(repo: str, skill_path: str = "", kind: str = "openclaw-skill",
                        expect_sha256: str = "") -> dict:
    """生态源条目：下载/打包成 .latiaoext → **落待确认区**（不直接安装）。

    ⑥（审计 2026-09-23）：此前这里把网络下载的内容打包成**本地临时文件**再交给
    install_extension —— 而"网络来源必须提供 sha256"的闸门只认 URL 形态，本地文件
    一律免检，于是整条 GitHub 安装路径绕过了摘要校验。现在改成：包摘要由服务端算，
    调用方（市场清单）若声明 expect_sha256 则必须一致；最后由用户确认时回传摘要
    （confirm_install），保证"下载的内容"与"确认安装的内容"是同一份字节。
    """
    from adapters import (install_openclaw_skill, install_claude_plugin,
                          parse_github_repo)
    repo = parse_github_repo(repo)
    if not repo:
        return {"status": "error", "message": f"无法识别仓库: {repo!r}"}
    logger.info("install github item: repo=%s skill_path=%s kind=%s", repo, skill_path or "(无)", kind)
    if kind.startswith("openclaw") and skill_path:
        zip_bytes = install_openclaw_skill(repo, skill_path)
    elif kind.startswith("claude"):
        zip_bytes = install_claude_plugin(repo)
    else:
        zip_bytes = install_openclaw_skill(repo, skill_path) if skill_path else install_claude_plugin(repo)
    if zip_bytes is None:
        logger.warning("install github item failed at download/pack: repo=%s skill_path=%s kind=%s",
                       repo, skill_path or "(无)", kind)
        return {"status": "error", "message": "下载/打包失败（源不可达或格式不符）"}
    label = skill_path or f"{repo}"
    src_desc = f"github:{repo}" + (f"/{skill_path}" if skill_path else "")
    return stage_package(zip_bytes, src_desc, expect_sha256=expect_sha256, label=label)


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
