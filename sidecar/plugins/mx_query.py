#!/usr/bin/env python3
"""mx_query - 妙想金融数据查询工具"""
import subprocess, sys, json, os, tempfile
from pathlib import Path

NAME = "mx_query"
PERMISSION = "safe"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "mx_query",
        "description": "查询【中国A股、港股、基金、板块、行业、指数】的实时行情、财务数据、资金流向等金融数据的专用工具，基于东方财富权威数据库。仅支持境内市场。⚠️适用判断：仅当用户询问的对象是 A股/港股/基金/板块/指数 时使用本工具（如：贵州茅台股价、A股大盘走势、恒生指数、某基金净值）。⚠️不支持的市场：美股、纳斯达克、道琼斯、标普、外汇、加密货币等一切境外/非证券市场——这类问题请直接改用 tavily_search，不要调用本工具，否则必然返回空数据。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "金融数据查询语句，如：贵州茅台2024年财务数据、查询A股大盘行情、宁德时代资金流向"
                }
            },
            "required": ["query"]
        }
    }
}

def execute(args: dict) -> str:
    """Execute a financial data query using the mx_data skill."""
    query = args.get("query", "")
    if not query:
        return "Error: query parameter is required"
    
    import multiprocessing

    if getattr(sys, "frozen", False):
        multiprocessing.freeze_support()
        try:
            result = subprocess.run(
                [sys.executable, "--mx-query", query],
                capture_output=True, text=True, timeout=120,
                env={**os.environ}
            )
            if result.returncode == 0:
                return result.stdout.strip() or "查询完成，无输出"
            detail = result.stderr.strip() or result.stdout.strip() or "无输出"
            return f"Error (exit {result.returncode}): {detail}。本工具仅支持 A股/港股/基金/板块/指数，美股等其它市场请改用 tavily_search 重试。"
        except subprocess.TimeoutExpired:
            return "Error: Query timed out (120s)"
        except Exception as e:
            return f"Error: {str(e)}"

    base = Path(__file__).resolve().parent
    mx_data = None
    for _ in range(6):
        cand = base / "skills" / "mx-data" / "mx_data.py"
        if cand.exists():
            mx_data = cand
            break
        base = base.parent
    if mx_data is None or not mx_data.exists():
        return "Error: mx_data.py not found (searched from plugins/ upward)"

    try:
        result = subprocess.run(
            [sys.executable, str(mx_data), "--query", query],
            capture_output=True, text=True, timeout=120,
            env={**os.environ}
        )
        if result.returncode == 0:
            return result.stdout.strip() or "查询完成，无输出"
        else:
            detail = result.stderr.strip() or result.stdout.strip() or "无输出"
            return f"Error (exit {result.returncode}): {detail}。本工具仅支持 A股/港股/基金/板块/指数，美股等其它市场请改用 tavily_search 重试。"
    except subprocess.TimeoutExpired:
        return "Error: Query timed out (120s)"
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    print(execute({"query": sys.argv[1] if len(sys.argv) > 1 else "测试"}))
