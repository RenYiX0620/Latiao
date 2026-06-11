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
        "description": "查询中国A股/港股/基金/板块/行业/指数的实时行情、财务数据、资金流向等金融数据的专用工具。基于东方财富权威数据库。当用户询问股票行情、大盘走势、基金净值、财务指标、资金流向等金融问题时，必须使用此工具，不要使用 tavily_search。",
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
    
    # Find mx_data.py
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
            return f"Error: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "Error: Query timed out (120s)"
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    print(execute({"query": sys.argv[1] if len(sys.argv) > 1 else "测试"}))
