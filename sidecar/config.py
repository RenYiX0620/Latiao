"""Shared configuration constants (single source of truth)."""
import os
from pathlib import Path

# LLM endpoints
LM_STUDIO_URL = os.environ.get("LATIAO_LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
# 未显式选模型时的兜底名：诚实标识本地引擎（不再冒充 gpt-4o-mini——
# 09-04 用户反馈"我就没用过这个模型名"，假名导致排查误判模型来源）
SUBAGENT_MODEL = os.environ.get("LATIAO_SUBAGENT_MODEL", "latiao-local-default")

# Paths
SKILLS_DIR = Path(__file__).parent / "skills"

# Application paths
# LATIAO_TEST_PROGRESS_DIR：测试专用覆盖——假引擎场景会真实执行循环并写入
# 进度等文件，不覆盖时会把真机 ~/.local-ai-os/PROGRESS.md 越写越大。
PROGRESS_DIR = Path(os.environ.get("LATIAO_TEST_PROGRESS_DIR", "")) \
    if os.environ.get("LATIAO_TEST_PROGRESS_DIR") else Path.home() / ".local-ai-os"

CONFIG_FILE = PROGRESS_DIR / "config.json"

# config.json 里是云端模型 key 与 Tavily key（⑤：审计要求 0600）。此前权限由
# 默认 umask 决定，实测是 0644 —— 同机任何用户/进程都能读到明文密钥。
CONFIG_MODE = 0o600


def save_config(cfg: dict, path: Path | None = None) -> Path:
    """写 config.json：临时文件 + os.replace（不出现半截文件），并强制 0600。

    唯一写入入口——此前 api_routes 有四处各写各的 `write_text`，权限与原子性
    全靠默认行为。先 os.open(..., 0o600) 再 chmod：替换后权限取自临时文件，
    所以必须显式设（umask 只会更严，不会替我们收紧到 0600）。
    """
    import json
    import os as _os
    target = Path(path) if path is not None else CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = _os.open(str(tmp), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, CONFIG_MODE)
    with _os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    try:
        _os.chmod(str(tmp), CONFIG_MODE)
    except OSError:
        pass
    _os.replace(str(tmp), str(target))
    return target


def harden_config_perms() -> list[str]:
    """把已存在的 config.json（含 .bak 备份）收紧到 0600，返回被改动的文件名。

    启动时调用：老版本装出来的文件仍是 0644，不补这一次就永远是 0644。
    只收紧不放松；文件不存在/只读等失败静默跳过。
    """
    import os as _os
    changed: list[str] = []
    try:
        candidates = list(PROGRESS_DIR.glob("config.json*"))
    except OSError:
        return changed
    for f in candidates:
        if not f.is_file():
            continue
        try:
            if _os.stat(str(f)).st_mode & 0o077:      # 组/其他用户有任何权限位
                _os.chmod(str(f), CONFIG_MODE)
                changed.append(f.name)
        except OSError:
            continue
    return changed
