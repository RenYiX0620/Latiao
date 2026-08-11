"""Write text content to a file. Creates parent directories if needed."""
import os

_BLOCKED_DIRS = ("/etc", "/System", "/usr", "/bin", "/sbin", "/var", "/private/etc")
_BLOCKED_SUBSTRINGS = ("/.ssh", "/.aws", "/.gnupg", "/Library/Keychains", "/.kube", "/.docker")
_BLOCKED_FILE_NAMES = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "known_hosts")

NAME = "write_file"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text content to a file. Creates parent directories if needed. ⚠️ Requires user confirmation before executing.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path where the file should be written."},
                "content": {"type": "string", "description": "The text content to write to the file."}
            },
            "required": ["path", "content"]
        }
    }
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    # 必须本身就是绝对路径（realpath 会把相对路径变成绝对路径，所以先查）
    if not os.path.isabs(expanded):
        return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    content = args["content"]
    p = _safe_path(args["path"])
    if p is None:
        return "⛔ Blocked: path traversal not allowed"
    # 系统目录一律拒绝写入
    if any(p == d or p.startswith(d + os.sep) for d in _BLOCKED_DIRS):
        return f"⛔ Blocked: 不允许写入系统目录 - {p}"
    # 敏感目录（密钥/凭证）一律拒绝
    if any(s in p for s in _BLOCKED_SUBSTRINGS):
        return f"⛔ Blocked: 不允许访问敏感目录 - {p}"
    # 敏感文件名一律拒绝
    if os.path.basename(p) in _BLOCKED_FILE_NAMES:
        return f"⛔ Blocked: 不允许写入敏感文件 - {p}"
    # sidecar 的 plugins/ 目录一律拒绝（写入后下次启动会被 import 执行 = RCE）
    sidecar_plugins = os.path.realpath(os.path.dirname(__file__))
    if p == sidecar_plugins or p.startswith(sidecar_plugins + os.sep):
        return f"⛔ Blocked: 不允许写入插件目录 - {p}"
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入：{p}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"
