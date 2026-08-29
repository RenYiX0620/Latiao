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
        resp = client.get(url)
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
        manifest = _read_manifest(Path(tmp))
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
            (Path(tmp) / f).exists() for f in ("plugin.py", "skills", "agents")
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
        for item in Path(tmp).iterdir():
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
