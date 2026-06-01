"""List the contents of a directory."""
import os

NAME = "list_dir"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the contents of a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the directory to list."}
            },
            "required": ["path"]
        }
    }
}


def execute(args: dict) -> str:
    path = args["path"]
    try:
        entries = os.listdir(path)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(path, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"
