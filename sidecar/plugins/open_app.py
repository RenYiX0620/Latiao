"""Open a macOS application by name. Supports both English and Chinese names."""
import subprocess

NAME = "open_app"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "open_app",
        "description": "Open a macOS application by name. Use this when the user asks to open an app. Supports both English names (Photos, Safari, Mail) and Chinese names (照片/相册, 浏览器, 邮件).",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "App name in English or Chinese (e.g., 'Photos', 'Safari', '照片', '浏览器')."}
            },
            "required": ["name"]
        }
    }
}

_APP_ALIASES = {
    "照片": "Photos", "相册": "Photos", "photo": "Photos",
    "音乐": "Music", "music": "Music",
    "浏览器": "Safari", "safari": "Safari",
    "邮件": "Mail", "mail": "Mail",
    "日历": "Calendar", "calendar": "Calendar",
    "备忘录": "Notes", "notes": "Notes",
    "提醒": "Reminders", "reminders": "Reminders",
    "计算器": "Calculator", "calculator": "Calculator",
    "终端": "Terminal", "terminal": "Terminal",
    "设置": "System Settings", "系统设置": "System Settings", "偏好设置": "System Settings",
    "App Store": "App Store", "app store": "App Store",
    "地图": "Maps", "maps": "Maps",
    "天气": "Weather", "weather": "Weather",
    "时钟": "Clock", "clock": "Clock",
    "查找": "Find My", "find my": "Find My",
}


def execute(args: dict) -> str:
    name = args["name"]
    resolved = _APP_ALIASES.get(name, name)
    try:
        r = subprocess.run(["open", "-a", resolved], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            err = r.stderr.strip() or "应用不存在或无法打开"
            return f"❌ 无法打开 {resolved}: {err}"
        return f"✅ 已打开应用：{resolved}"
    except subprocess.TimeoutExpired:
        return f"❌ 打开 {resolved} 超时"
    except Exception as e:
        return f"无法打开应用 {resolved}: {e}"
