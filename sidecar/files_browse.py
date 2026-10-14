"""模型目录选择器：应用内浏览文件系统。

绕开 macOS 原生目录面板"双击=进入、单击+打开才选中"的歧义——
在应用内点文件夹进入、点按钮选中当前目录，行为与平台无关。
"""
import os
from pathlib import Path

from fastapi import HTTPException, Query

from main import app


def _model_roots() -> list[tuple[str, str]]:
    """[(path, label)] — 目录选择器快捷入口。全部基于 Path.home() 动态计算，
    换设备/换用户名自动适配，无需任何配置。"""
    home = Path.home()
    return [
        (str(home / "Models"), "Models"),
        (str(home / ".lmstudio" / "models"), "LM Studio"),
        (str(home), "主目录"),
    ]


@app.get("/v1/files/browse")
async def files_browse(path: str = Query(default="")):
    """列出目录内容。默认从模型目录根开始。

    访问范围（2026-10-11 第三轮审计修复）：此前 resolve 后直接 iterdir，
    任意目录都能列；现在限定在"模型根 + 家目录"之内，且拒绝密钥/凭据类目录
    （用 sensitive_read_block 的目录名单，读与列共用一份判据）。
    """
    roots = [(p, label) for p, label in _model_roots() if os.path.isdir(p)]
    if not path:
        path = roots[0][0] if roots else str(Path.home())
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="路径无效") from None
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="不是目录") from None
    # 范围限制：必须在某个模型根之内（家目录是其中一个根，故家目录下均可浏览）
    _allowed = False
    for rp, _label in roots:
        try:
            _rrp = Path(rp).expanduser().resolve()
        except Exception:
            continue
        if p == _rrp or _rrp in p.parents:
            _allowed = True
            break
    if not _allowed:
        raise HTTPException(status_code=403, detail="该目录不在可浏览范围内")
    # 敏感目录（密钥/凭据）不列
    try:
        from cmd_safety import sensitive_read_block
        _blk = sensitive_read_block(str(p))
        if _blk:
            raise HTTPException(status_code=403, detail="该目录受保护")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        children = list(p.iterdir())
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权限访问该目录") from None
    entries = []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": str(child),
            "is_dir": is_dir,
            "size": 0 if is_dir else (child.stat().st_size if child.is_file() else 0),
        })
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {
        "status": "ok",
        "path": str(p),
        "parent": None if str(p) == str(p.parent) else str(p.parent),
        "roots": [{"path": rp, "label": label} for rp, label in roots],
        "entries": entries[:500],
    }
