"""模型目录选择器：应用内浏览文件系统。

绕开 macOS 原生目录面板"双击=进入、单击+打开才选中"的歧义——
在应用内点文件夹进入、点按钮选中当前目录，行为与平台无关。
"""
import os
from pathlib import Path

from fastapi import HTTPException, Query

from main import app


def _model_roots() -> list[str]:
    home = Path.home()
    return [
        str(home / "Models"),
        str(home / ".lmstudio" / "models"),
        str(home),
    ]


@app.get("/v1/files/browse")
async def files_browse(path: str = Query(default="")):
    """列出目录内容。默认从模型目录根开始。"""
    roots = [r for r in _model_roots() if os.path.isdir(r)]
    if not path:
        path = roots[0] if roots else str(Path.home())
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="路径无效")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")
    try:
        children = list(p.iterdir())
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权限访问该目录")
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
        "roots": roots,
        "entries": entries[:500],
    }
