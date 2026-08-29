"""Read the contents of a file at the given path. Supports ~ expansion."""

import os

_BLOCKED_SUBSTRINGS = ("/.ssh", "/.aws", "/.gnupg", "/Library/Keychains", "/.kube", "/.docker")
_BLOCKED_FILE_NAMES = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "known_hosts")

MAX_READ_SIZE = 10000  # chars before truncation

NAME = "read_file"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file at the given path. Large files are truncated.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."}
            },
            "required": ["path"]
        }
    }
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        # 相对路径按 sidecar 工作目录解析（模型常发 "." 或相对路径，
        # 此前直接拒绝并误报"路径穿越"）
        try:
            from agent_loop import _safe_cwd
            base = _safe_cwd()
            if not base:
                return None
            expanded = os.path.join(base, expanded)
        except Exception:
            return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args["path"])
    if p is None:
        return "⛔ Blocked: 路径无效（空路径或包含 .. 穿越片段）"
    # 敏感目录（密钥/凭证）一律拒绝
    if any(s in p for s in _BLOCKED_SUBSTRINGS):
        return f"⛔ Blocked: 不允许访问敏感目录 - {p}"
    # 敏感文件名一律拒绝
    if os.path.basename(p) in _BLOCKED_FILE_NAMES:
        return f"⛔ Blocked: 不允许读取敏感文件 - {p}"
    try:
        # 先检测是否为二进制文件(xlsx/zip/png 等),避免 utf-8 codec 报错
        # 让模型困惑。读前 1KB 探测 NUL 字节或已知二进制魔数。
        _BINARY_EXTS = {".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".gif",
                        ".bmp", ".webp", ".zip", ".gz", ".tar", ".7z", ".rar",
                        ".mp3", ".mp4", ".mov", ".avi", ".woff", ".woff2",
                        ".ttf", ".otf", ".icns", ".ico", ".class", ".so", ".dylib", ".dll", ".exe"}
        ext = os.path.splitext(p)[1].lower()
        is_binary = ext in _BINARY_EXTS
        if not is_binary:
            # 探测文件内容:前 1024 字节含 NUL -> 二进制
            with open(p, "rb") as bf:
                head = bf.read(1024)
            if b"\x00" in head:
                is_binary = True
        if is_binary:
            return (f"⚠️ 这是二进制文件({ext or '未知格式'}),无法作为文本读取。\n"
                    f"文件: {p}\n"
                    f"如需查看数据,请读取对应的文本格式文件(如 _raw.json / _description.txt)。")

        with open(p, "r", encoding="utf-8") as f:
            content = f.read(MAX_READ_SIZE + 1)
        if len(content) > MAX_READ_SIZE:
            est_lines = content.count("\n")
            return (
                content[:MAX_READ_SIZE]
                + f"\n\n... (文件过长，已截断。约 {est_lines}+ 行，"
                + f"仅显示前 {MAX_READ_SIZE} 字符。如需完整内容请分段读取)"
            )
        return content
    except FileNotFoundError:
        return f"错误：文件不存在 - {p}"
    except UnicodeDecodeError:
        return (f"⚠️ 文件编码不是 UTF-8,无法作为文本读取。\n"
                f"文件: {p}\n"
                f"如需查看数据,请读取对应的文本格式文件(如 _raw.json / _description.txt)。")
    except Exception as e:
        return f"错误：{e}"
