"""Read the contents of a file at the given path."""

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


def execute(args: dict) -> str:
    path = args["path"]
    # Block path traversal — only allow absolute paths without '..' segments
    if ".." in path.split("/"):
        return "⛔ Blocked: path traversal not allowed"
    try:
        with open(path, "r", encoding="utf-8") as f:
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
        return f"错误：文件不存在 - {path}"
    except Exception as e:
        return f"错误：{e}"
