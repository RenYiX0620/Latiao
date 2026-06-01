"""Write text content to a file. Creates parent directories if needed."""
import os

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


def execute(args: dict) -> str:
    path = args["path"]
    content = args["content"]
    # Block path traversal — only allow absolute paths without '..' segments
    if ".." in path.split("/"):
        return "⛔ Blocked: path traversal not allowed"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入：{path}（{len(content)} 字符）"
    except Exception as e:
        return f"错误：{e}"
