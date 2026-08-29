"""capability_registry — 统一能力模型：工具与技能合并为一张能力表。

设计（对齐 ZCode 能力生态）：
- 单一事实源：memory.db 的 capabilities 表（kind: tool | skill）
- 工具 = 代码插件：执行代码留在 agent_loop 的内存 dispatch，本表管理
  其目录/开关/权限/使用计数的唯一事实源
- 技能 = markdown 提示词：正文存表，运行时经 use_skill 工具按需调用
- source 三类：builtin（内置）/ extension:<名>（扩展包）/ user（用户自建）
- 一次性迁移：旧 skills.json 开关 → enabled；permissions.json 无路径规则
  → permission（path 级规则保留在 permissions.json，解析时仍优先）

本模块不 import main/agent_loop（避免循环依赖）；只依赖 config/db/extension_manager。
"""
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from config import PROGRESS_DIR
from db import _get_db, _init_db

# 表可能尚不存在：agent_loop 导入时的能力同步早于 main lifespan 的 _init_db，
# 故模块级确保建表（CREATE TABLE IF NOT EXISTS，幂等）
_init_db()

logger = logging.getLogger(__name__)

BUILTIN_SKILLS_DIR = Path(__file__).parent / "skills"   # 内置技能种子：<dir>/SKILL.md
USER_SKILLS_DIR = PROGRESS_DIR / "skills"                # 用户技能读写字目录
_MIGRATION_MARKER = PROGRESS_DIR / ".capabilities_migrated"

_VALID_PERMS = ("safe", "confirm", "danger", "deny")
_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_write_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").lower()).strip("-")[:40]
    if not slug:
        # 纯中文等非 ASCII 名无法 slug 化 → 用稳定哈希兜底
        import hashlib
        slug = "skill-" + hashlib.md5((name or "").encode("utf-8")).hexdigest()[:8]
    return slug


def _exec(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _write_lock:
        conn = _get_db()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def _query(sql: str, params: tuple = ()) -> list[tuple]:
    with _write_lock:
        return _get_db().execute(sql, params).fetchall()


def _upsert_row(name: str, kind: str, display_name: str, description: str = "",
                definition: str | dict = "{}", content: str = "", permission: str = "safe",
                source: str = "builtin", source_path: str = "", perm_override: int = 0,
                preserve_state: bool = True) -> None:
    """upsert 能力行。preserve_state=True 时保留已有 enabled/usage_count；
    perm_override=1 表示该权限是用户覆盖（优先于插件默认）。"""
    if isinstance(definition, dict):
        definition = json.dumps(definition, ensure_ascii=False)
    if permission not in _VALID_PERMS:
        permission = "safe"
    with _write_lock:
        conn = _get_db()
        row = conn.execute("SELECT enabled, usage_count FROM capabilities WHERE name=?",
                           (name,)).fetchone()
        if row is not None and preserve_state:
            enabled, usage = row[0], row[1]
        else:
            enabled, usage = 1, 0
        conn.execute(
            "INSERT INTO capabilities (name, kind, display_name, description, definition, "
            "content, permission, perm_override, enabled, source, source_path, usage_count, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, display_name=excluded.display_name, "
            "description=excluded.description, definition=excluded.definition, "
            "content=excluded.content, permission=excluded.permission, "
            "perm_override=excluded.perm_override, source=excluded.source, "
            "source_path=excluded.source_path, updated_at=excluded.updated_at",
            (name, kind, display_name, description, definition, content, permission,
             perm_override, enabled, source, source_path, usage, _now(), _now()))
        conn.commit()


# ═══════════════════════════════════════════════════════
#  同步：插件加载结果 / 技能文件 → 表
# ═══════════════════════════════════════════════════════

def _extension_module_map() -> dict[str, str]:
    """{模块名: 扩展名}——工具 callable 的 __module__ 形如 plugin_<ext>_<ver>。"""
    out = {}
    try:
        from extension_manager import active_extension_dirs
        for ext_dir in active_extension_dirs():
            out[f"plugin_{ext_dir.parent.name}_{ext_dir.name}"] = ext_dir.parent.name
    except Exception:
        logger.warning("extension manager unavailable for capability sync", exc_info=True)
    return out


def _tool_source(func, ext_map: dict[str, str]) -> str:
    mod = getattr(func, "__module__", "") or ""
    if mod in ext_map:
        return f"extension:{ext_map[mod]}"
    return "builtin"


def _read_perm_state(name: str) -> tuple[str, int]:
    """读权限列与覆盖标记。返回 (permission, perm_override)。"""
    try:
        rows = _query(
            "SELECT permission, perm_override FROM capabilities WHERE name=?", (name,))
        if rows:
            return rows[0][0], int(rows[0][1])
    except Exception:
        logger.debug("_read_perm_state failed for %s", name, exc_info=True)
    return "safe", 0


def sync_tools(tools: list[dict], permissions: dict[str, str], dispatch: dict,
               prune: bool = False) -> int:
    """把插件注册表 upsert 进能力表。prune=True 时删除已不在 dispatch 中的工具行。
    权限：用户覆盖（perm_override=1）优先保留，否则同步插件默认值。"""
    ext_map = _extension_module_map()
    names = set()
    for tool_def in tools or []:
        if not isinstance(tool_def, dict):
            continue
        fn = tool_def.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        names.add(name)
        func = dispatch.get(name) if dispatch else None
        source = _tool_source(func, ext_map) if func is not None else "builtin"
        cur_perm, cur_ov = _read_perm_state(name)
        if cur_ov == 1:
            perm, override = cur_perm, 1
        else:
            perm = permissions.get(name, "safe") if permissions else "safe"
            override = 0
        _upsert_row(
            name=name, kind="tool", display_name=name,
            description=(fn.get("description") or "").strip(),
            definition=tool_def, permission=perm,
            source=source, source_path=getattr(func, "__module__", "") or "",
            perm_override=override,
        )
    if prune:
        try:
            with _write_lock:
                conn = _get_db()
                rows = conn.execute("SELECT name FROM capabilities WHERE kind='tool'").fetchall()
                stale = [r[0] for r in rows if r[0] not in names]
                if stale:
                    conn.executemany("DELETE FROM capabilities WHERE name=?", [(n,) for n in stale])
                    conn.commit()
                    logger.info("capability sync 清理过期工具行: %s", stale)
        except Exception:
            logger.warning("capability tool prune failed", exc_info=True)
    return len(names)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """解析 YAML frontmatter。返回 (meta, body)。无 frontmatter 返回 ({}, 原文)。"""
    if not content.startswith("---"):
        return {}, content.strip()
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content.strip()
    try:
        import yaml
        meta = yaml.load(parts[1], Loader=yaml.SafeLoader)
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        logger.warning("skill frontmatter parse failed", exc_info=True)
        meta = {}
    return meta, parts[2].strip()


def _first_desc_line(body: str) -> str:
    for line in (body or "").split("\n"):
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---"):
            return s[:120]
    return ""


def sync_skills() -> int:
    """三来源技能统一入表：内置子目录 SKILL.md / 扩展包 skills/*.md / 用户目录 *.md。"""
    seen: set[str] = set()

    # 1) 内置：<dir>/SKILL.md（frontmatter: name/description/security_level）
    if BUILTIN_SKILLS_DIR.exists():
        for skill_dir in sorted(BUILTIN_SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                meta, body = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
                name = str(meta.get("name") or skill_dir.name)
                seen.add(name)
                cur_perm, cur_ov = _read_perm_state(name)
                if cur_ov == 1:
                    perm, override = cur_perm, 1
                else:
                    perm = str(meta.get("security_level") or "safe")
                    override = 0
                _upsert_row(
                    name=name, kind="skill",
                    display_name=str(meta.get("name") or skill_dir.name.replace("-", " ").title()),
                    description=str(meta.get("description") or _first_desc_line(body)),
                    content=body, permission=perm,
                    source="builtin", source_path=str(skill_md), perm_override=override,
                )
            except Exception:
                logger.warning("Failed to sync builtin skill %s", skill_dir.name, exc_info=True)

    # 2) 扩展包：<ext>/<version>/skills/*.md
    try:
        from extension_manager import active_extension_dirs
        for ext_dir in active_extension_dirs():
            ext_name = ext_dir.parent.name
            skills_d = ext_dir / "skills"
            if not skills_d.is_dir():
                continue
            for f in sorted(skills_d.rglob("*.md")):
                try:
                    meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
                    name = str(meta.get("name") or f.stem)
                    seen.add(name)
                    cur_perm, cur_ov = _read_perm_state(name)
                    if cur_ov == 1:
                        perm, override = cur_perm, 1
                    else:
                        perm = str(meta.get("security_level") or "safe")
                        override = 0
                    _upsert_row(
                        name=name, kind="skill",
                        display_name=str(meta.get("name") or f.stem.replace("-", " ").title()),
                        description=str(meta.get("description") or _first_desc_line(body)),
                        content=body, permission=perm,
                        source=f"extension:{ext_name}", source_path=str(f), perm_override=override,
                    )
                except Exception:
                    logger.warning("Failed to sync extension skill %s", f, exc_info=True)
    except Exception:
        logger.warning("Extension skills sync failed", exc_info=True)

    # 3) 用户技能：~/.local-ai-os/skills/*.md（表为事实源，文件是持久载体）
    if USER_SKILLS_DIR.exists():
        for f in sorted(USER_SKILLS_DIR.glob("*.md")):
            if f.name.lower().startswith("readme"):
                continue
            try:
                name = f.stem
                seen.add(name)
                body = f.read_text(encoding="utf-8").strip()
                _upsert_row(
                    name=name, kind="skill",
                    display_name=name.replace("-", " ").title(),
                    description=_first_desc_line(body), content=body,
                    permission="safe", source="user", source_path=str(f),
                )
            except Exception:
                logger.warning("Failed to sync user skill %s", f, exc_info=True)

    # 清理：builtin/extension 源的文件已消失的行
    try:
        with _write_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT name, source FROM capabilities WHERE kind='skill' "
                "AND source IN ('builtin') OR (kind='skill' AND source LIKE 'extension:%')"
            ).fetchall()
            stale = [r[0] for r in rows if r[0] not in seen]
            if stale:
                conn.executemany("DELETE FROM capabilities WHERE name=?", [(n,) for n in stale])
                conn.commit()
                logger.info("capability sync 清理过期技能行: %s", stale)
    except Exception:
        logger.warning("skill prune failed", exc_info=True)
    return len(seen)


def initialize(tools: list[dict], permissions: dict[str, str], dispatch: dict) -> dict:
    """启动时完整同步 + 一次性旧数据迁移。幂等，可重复调用。
    顺序：先建行 → 迁移（开关/权限/移动旧文件）→ 再同步（新迁移文件入表）。"""
    n_tools = sync_tools(tools, permissions, dispatch, prune=True)
    n_skills = sync_skills()
    _migrate_legacy()
    n_skills = sync_skills()
    return {"tools": n_tools, "skills": n_skills}


# ═══════════════════════════════════════════════════════
#  查询 / 开关 / 权限 / 计数
# ═══════════════════════════════════════════════════════

def list_capabilities(kind: str | None = None) -> list[dict]:
    sql = "SELECT name, kind, display_name, description, permission, enabled, source, usage_count FROM capabilities"
    params: tuple = ()
    if kind in ("tool", "skill"):
        sql += " WHERE kind=?"
        params = (kind,)
    sql += " ORDER BY kind, name"
    try:
        rows = _query(sql, params)
    except Exception:
        logger.warning("list capabilities failed", exc_info=True)
        return []
    return [
        {"name": r[0], "kind": r[1], "display_name": r[2], "description": r[3],
         "permission": r[4], "enabled": bool(r[5]), "source": r[6], "usage_count": r[7]}
        for r in rows
    ]


def get_capability(name: str) -> dict | None:
    try:
        rows = _query(
            "SELECT name, kind, display_name, description, permission, enabled, source, "
            "source_path, usage_count FROM capabilities WHERE name=?", (name,))
    except Exception:
        return None
    if not rows:
        return None
    r = rows[0]
    return {"name": r[0], "kind": r[1], "display_name": r[2], "description": r[3],
            "permission": r[4], "enabled": bool(r[5]), "source": r[6],
            "source_path": r[7] or "", "usage_count": r[8]}


def get_permission(name: str) -> str | None:
    """返回用户覆盖的权限；无覆盖（插件默认）返回 None。
    _resolve_permission 据此回落到 TOOL_PERMISSIONS 插件默认。"""
    try:
        rows = _query(
            "SELECT permission, perm_override FROM capabilities WHERE name=?", (name,))
        if rows and int(rows[0][1]) == 1:
            return rows[0][0]
    except Exception:
        logger.debug("get_permission failed for %s", name, exc_info=True)
    return None


def set_enabled(name: str, enabled: bool) -> dict | None:
    _exec("UPDATE capabilities SET enabled=?, updated_at=? WHERE name=?",
          (1 if enabled else 0, _now(), name))
    return get_capability(name)


def set_permission(name: str, permission: str) -> dict | None:
    if permission not in _VALID_PERMS:
        return None
    _exec("UPDATE capabilities SET permission=?, perm_override=1, updated_at=? WHERE name=?",
          (permission, _now(), name))
    return get_capability(name)


def bump_usage(name: str) -> None:
    try:
        _exec("UPDATE capabilities SET usage_count=usage_count+1 WHERE name=?", (name,))
    except Exception:
        logger.debug("bump_usage failed for %s", name, exc_info=True)


def get_skill_content(name: str) -> dict | None:
    """运行时取技能全文。返回 {name, description, content, permission}；未启用/不存在返回 None。"""
    try:
        rows = _query(
            "SELECT content, description, permission FROM capabilities "
            "WHERE kind='skill' AND name=? AND enabled=1", (name,))
    except Exception:
        return None
    if not rows:
        return None
    r = rows[0]
    return {"name": name, "description": r[1] or "", "content": r[0] or "", "permission": r[2] or "safe"}


def skill_catalog() -> list[dict]:
    """已启用技能目录（名字 + 一句话描述），注入 system prompt 引导 use_skill 调用。"""
    try:
        rows = _query(
            "SELECT name, display_name, description FROM capabilities "
            "WHERE kind='skill' AND enabled=1 ORDER BY name")
    except Exception:
        return []
    return [{"name": r[0], "display_name": r[1], "description": r[2] or ""} for r in rows]


# ═══════════════════════════════════════════════════════
#  用户技能 CRUD（双写：表 + ~/.local-ai-os/skills/<key>.md）
# ═══════════════════════════════════════════════════════

def create_skill(display_name: str, content: str) -> dict | None:
    name = _slug(display_name)
    if not name or not (content or "").strip():
        return None
    existing = get_capability(name)
    if existing is not None:
        return None
    try:
        USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        filepath = USER_SKILLS_DIR / f"{name}.md"
        filepath.write_text(content.strip(), encoding="utf-8")
        _upsert_row(name=name, kind="skill", display_name=display_name.strip() or name,
                    description=_first_desc_line(content.strip()), content=content.strip(),
                    permission="safe", source="user", source_path=str(filepath))
        return get_capability(name)
    except Exception:
        logger.warning("create_skill failed", exc_info=True)
        return None


def delete_skill(name: str) -> bool:
    row = get_capability(name)
    if row is None or row["kind"] != "skill" or row["source"] != "user":
        return False
    try:
        _exec("DELETE FROM capabilities WHERE name=?", (name,))
        sp = row.get("source_path") or ""
        if sp:
            p = Path(sp)
            if p.exists():
                p.unlink()
        return True
    except Exception:
        logger.warning("delete_skill failed for %s", name, exc_info=True)
        return False


def remove_extension_caps(ext_name: str) -> int:
    """卸载扩展时移除其工具/技能能力行。"""
    try:
        with _write_lock:
            conn = _get_db()
            cur = conn.execute("DELETE FROM capabilities WHERE source=?", (f"extension:{ext_name}",))
            conn.commit()
            return cur.rowcount
    except Exception:
        logger.warning("remove_extension_caps failed for %s", ext_name, exc_info=True)
        return 0


# ═══════════════════════════════════════════════════════
#  一次性迁移：旧 skills.json / permissions.json / 扁平技能文件
# ═══════════════════════════════════════════════════════

def _migrate_legacy() -> None:
    if _MIGRATION_MARKER.exists():
        return
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        # 1) 旧扁平技能文件（sidecar/skills/*.md，无 frontmatter 的卡片）→ 用户目录
        #    README 等文档除外
        moved = 0
        if BUILTIN_SKILLS_DIR.exists():
            USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            for f in sorted(BUILTIN_SKILLS_DIR.glob("*.md")):
                if f.name.lower().startswith("readme"):
                    continue
                dest = USER_SKILLS_DIR / f.name
                if dest.exists():
                    continue
                try:
                    dest.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
                    f.unlink()
                    moved += 1
                except Exception:
                    logger.warning("migrate skill file failed: %s", f, exc_info=True)
        # 2) skills.json 开关 → enabled（key 即文件 stem / tavily_search）
        cfg_file = PROGRESS_DIR / "skills.json"
        if cfg_file.exists():
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            for key, val in cfg.items():
                if isinstance(val, dict) and "enabled" in val:
                    _exec("UPDATE capabilities SET enabled=? WHERE name=?",
                          (1 if val["enabled"] else 0, key))
        # 3) permissions.json 无 path_pattern 的规则 → permission（用户覆盖，path 规则留在文件）
        perm_file = PROGRESS_DIR / "permissions.json"
        if perm_file.exists():
            data = json.loads(perm_file.read_text(encoding="utf-8"))
            rules = data.get("rules", []) if isinstance(data, dict) else []
            remaining = []
            for rule in rules:
                if not isinstance(rule, dict):
                    remaining.append(rule)
                    continue
                tool, perm = rule.get("tool"), rule.get("permission")
                if tool and perm and not rule.get("path_pattern"):
                    _exec("UPDATE capabilities SET permission=?, perm_override=1 WHERE name=?",
                          (perm, tool))
                else:
                    remaining.append(rule)
            data["rules"] = remaining
            perm_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        _MIGRATION_MARKER.write_text(_now(), encoding="utf-8")
        if moved:
            logger.info("capability migration: moved %d legacy skill files", moved)
        logger.info("capability migration complete")
    except Exception:
        logger.warning("capability legacy migration failed", exc_info=True)
