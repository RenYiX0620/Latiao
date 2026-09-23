"""进程内秘密持有（2026-09-23 审计后补，配合 token 与妙想 key 搬出环境）。

为什么"读到内存后从 os.environ 删掉"不够：macOS 的 `ps eww` / `ps -E` 读的是进程
**exec 时**的环境快照——实测 pop 之后本进程 os.environ 里已经没有该键了，ps 仍然
显示它（命中 1 次）。所以凭据只能：
  1. 一开始就不在环境里（应用 token：Rust 侧走子进程 stdin）；
  2. 或在 0600 的配置里存着，启动时读进内存、需要时显式注入给子进程（妙想 key）。

本模块就是 (2) 的落点：一个进程内存字典 + 从 config.json 读取已知密钥字段。
不要把它当通用配置读——只放"本该在钥匙串/0600 文件里"的凭据。
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger("latiao-sidecar")

# 环境变量名 → config.json 里的字段名（迁移期两种来源都认）
_CONFIG_FIELDS = {"MX_APIKEY": "mx_api_key"}

_SECRETS: dict[str, str] = {}


def put(name: str, value: str) -> None:
    if value:
        _SECRETS[name] = value


def get(name: str, default: str = "") -> str:
    return _SECRETS.get(name, default)


def has(name: str) -> bool:
    return bool(_SECRETS.get(name))


def adopt(name: str, value: str | None = None) -> bool:
    """把凭据收进内存，并**从环境移除**（迁移期用：老用户仍靠环境变量提供）。

    value 未给则读环境；无论有没有取到都移除环境里的同名键——留在环境里等于
    让同机任何进程（包括模型自己跑的命令）都能通过 ps 读到。
    """
    v = value if value is not None else os.environ.get(name, "")
    if v:
        _SECRETS[name] = v
    os.environ.pop(name, None)
    return bool(v)


def load_from_config_file(path: Path | str | None) -> list[str]:
    """从 config.json 读取已知凭据字段进内存。返回本次载入的键名列表。"""
    if not path:
        return []
    import json
    p = Path(path)
    try:
        if not p.is_file():
            return []
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("读取 config 里的凭据字段失败: %s", p, exc_info=True)
        return []
    loaded: list[str] = []
    for env_name, field in _CONFIG_FIELDS.items():
        val = str(cfg.get(field, "") or "").strip()
        if val:
            _SECRETS[env_name] = val
            loaded.append(env_name)
    return loaded


def reset_for_tests() -> None:
    _SECRETS.clear()
