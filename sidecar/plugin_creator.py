"""NL 造插件（plugin-creator）：把"一句话描述"变成可安装的 .latiaoext。

安全模型（必须遵守的既有红线）：
- `sidecar/plugins/` 是启动即 import 的目录——**本模块绝不直接往那里写代码**；
- 一切生成物都先经静态校验 + 结构沙箱检查，再打包成 .latiaoext，
  交由 `extension_manager.install_extension()` 走既有安装链（zip 安全解压、
  manifest 校验、权限分级、sha256 记录）；
- **不 import、不执行**生成物：只做 AST 静态分析（导入即执行 = 任意代码执行）。

三层校验：
1. 结构沙箱：包内 manifest 合法（name/version/permissions ∈ 枚举）、无路径逃逸、体积上限；
2. AST 校验：plugin.py 必须定义 NAME/DEFINITION/PERMISSION/execute，且不含
   eval/exec/__import__/compile/os.system/os.popen 等直接执行入口；
3. 权限推断：代码用到 subprocess/socket/requests 等能力时，声明权限不得低于推断值
   （防"manifest 写只读、代码实际能发网络/执行命令"）。
"""
from __future__ import annotations

import ast
import io
import re
import zipfile

_VALID_PERMS = {"readonly", "files", "network", "shell"}
_PERM_RANK = {"readonly": 0, "files": 1, "network": 2, "shell": 3}

# 直接执行/反射入口：插件里出现即拒绝（不是"危险权限"，是"绕过权限体系"）
_FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__",
    "os.system", "os.popen", "os.execv", "os.execve", "os.spawnv",
    "pty.spawn", "importlib.import_module", "globals", "locals",
}
_SHELL_HINTS = ("subprocess", "pty", "os.system", "os.popen", "shutil")
_NET_HINTS = ("requests", "httpx", "socket", "urllib", "aiohttp", "websocket")
_FILE_HINTS = ("open", "pathlib", "shutil", "os.remove", "os.rename", "os.makedirs")

_MAX_PKG_BYTES = 5 * 1024 * 1024
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{1,40}$")


def _safe_name(name: str) -> str:
    n = re.sub(r"[^a-zA-Z0-9_-]", "-", (name or "").strip()).strip("-")
    return n or "unnamed"


def validate_plugin_code(code: str) -> dict:
    """AST 静态校验插件源码。返回 {ok, errors[], inferred_permission}。"""
    errors: list[str] = []
    inferred = "readonly"
    try:
        tree = ast.parse(code or "")
    except SyntaxError as e:
        return {"ok": False, "errors": [f"语法错误：{e}"], "inferred_permission": "shell"}

    assigned = set()
    has_execute = False
    used_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    assigned.add(tgt.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute":
            has_execute = True
        elif isinstance(node, ast.Name):
            used_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node.value.id if isinstance(node.value, ast.Name) else ""
            if base:
                used_names.add(f"{base}.{node.attr}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                if fn.id in _FORBIDDEN_CALLS:
                    errors.append(f"禁止的调用：{fn.id}()（绕过权限体系）")
            elif isinstance(fn, ast.Attribute):
                base = fn.value.id if isinstance(fn.value, ast.Name) else ""
                full = f"{base}.{fn.attr}" if base else fn.attr
                if full in _FORBIDDEN_CALLS:
                    errors.append(f"禁止的调用：{full}()（绕过权限体系）")

    for need in ("NAME", "DEFINITION", "PERMISSION"):
        if need not in assigned:
            errors.append(f"缺少模块级变量：{need}")
    if not has_execute:
        errors.append("缺少 execute(args: dict) -> str 函数")

    # 权限推断：用到的能力 → 最低权限
    low = code.lower()
    if any(h in low for h in _SHELL_HINTS):
        inferred = "shell"
    elif any(h in low for h in _NET_HINTS):
        inferred = "network"
    elif "open(" in low or any(h in low for h in _FILE_HINTS):
        inferred = "files"
    return {"ok": not errors, "errors": errors, "inferred_permission": inferred}


def build_skill_package(name: str, description: str, instructions: str,
                        security_level: str = "safe") -> tuple[bytes, dict]:
    """生成技能包（manifest + skills/<name>/SKILL.md）。"""
    nm = _safe_name(name)
    if not _NAME_RE.match(nm):
        raise ValueError(f"非法名称：{name!r}（需 2-41 位字母/数字/-/_）")
    body = (instructions or "").strip()
    if len(body) < 20:
        raise ValueError("技能说明太短（至少 20 字，需包含触发场景与执行步骤）")
    front = (f"---\nname: {nm}\ndescription: {description.strip()[:200] or nm}\n"
             f"security_level: {security_level}\n---\n\n")
    manifest = (f"name: {nm}\nversion: 1.0.0\n"
                f"description: {description.strip()[:200] or nm}\n"
                f"author:\n  name: local\npermissions:\n  - readonly\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.yaml", manifest)
        zf.writestr(f"skills/{nm}/SKILL.md", front + body + "\n")
    meta = {"kind": "skill", "name": nm, "permissions": ["readonly"], "size": buf.tell()}
    return buf.getvalue(), meta


def build_plugin_package(name: str, description: str, code: str,
                         permission: str, instructions: str = "") -> tuple[bytes, dict]:
    """生成工具插件包（manifest + plugin.py [+ 可选 SKILL.md]）。"""
    nm = _safe_name(name)
    if not _NAME_RE.match(nm):
        raise ValueError(f"非法名称：{name!r}")
    permission = (permission or "readonly").strip().lower()
    if permission not in _VALID_PERMS:
        raise ValueError(f"非法权限：{permission!r}（可选 {sorted(_VALID_PERMS)}）")
    v = validate_plugin_code(code)
    if not v["ok"]:
        raise ValueError("插件代码未通过校验：" + "；".join(v["errors"]))
    need_rank = _PERM_RANK.get(v["inferred_permission"], 3)
    if _PERM_RANK[permission] < need_rank:
        raise ValueError(
            f"权限不足：代码需要 {v['inferred_permission']}，但声明为 {permission}"
            "（防“声明只读、实际能联网/执行命令”）")

    manifest = (f"name: {nm}\nversion: 1.0.0\n"
                f"description: {description.strip()[:200] or nm}\n"
                f"author:\n  name: local\npermissions:\n  - {permission}\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.yaml", manifest)
        zf.writestr("plugin.py", code)
        if instructions.strip():
            front = (f"---\nname: {nm}\ndescription: {description.strip()[:200] or nm}\n"
                     f"security_level: safe\n---\n\n")
            zf.writestr(f"skills/{nm}/SKILL.md", front + instructions.strip() + "\n")
    meta = {"kind": "plugin", "name": nm, "permissions": [permission],
            "inferred_permission": v["inferred_permission"], "size": buf.tell()}
    return buf.getvalue(), meta


def sandbox_check(zip_bytes: bytes) -> dict:
    """结构沙箱检查：体积、路径逃逸、manifest 合法性（不执行任何内容）。"""
    if len(zip_bytes) > _MAX_PKG_BYTES:
        return {"ok": False, "errors": [f"包体积超限（{len(zip_bytes)//1024}KB > 5MB）"]}
    errors: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            for n in names:
                if n.startswith("/") or ".." in n.split("/"):
                    errors.append(f"路径逃逸：{n}")
            if "manifest.yaml" not in names:
                errors.append("缺少 manifest.yaml")
            else:
                mf = zf.read("manifest.yaml").decode("utf-8", "ignore")
                if not re.search(r"^name:\s*\S+", mf, re.M):
                    errors.append("manifest 缺少 name")
                if not re.search(r"^version:\s*\S+", mf, re.M):
                    errors.append("manifest 缺少 version")
                perms = re.findall(r"^\s*-\s*(\w+)\s*$", mf.split("permissions:")[-1], re.M)
                bad = [p for p in perms if p not in _VALID_PERMS]
                if bad:
                    errors.append(f"manifest 权限非法：{bad}")
            for n in names:
                if n.endswith(".py"):
                    r = validate_plugin_code(zf.read(n).decode("utf-8", "ignore"))
                    if not r["ok"]:
                        errors.extend(f"{n}: {e}" for e in r["errors"])
    except zipfile.BadZipFile:
        errors.append("不是合法的 zip 包")
    return {"ok": not errors, "errors": errors}
