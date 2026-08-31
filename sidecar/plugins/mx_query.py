#!/usr/bin/env python3
"""mx_query - 妙想金融数据查询工具"""
import os
import subprocess
import sys
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

# 接口对查询措辞敏感：某些问法（如"本周涨跌幅表现"）东方财富解析不出结果，
# 返回"无 dataTableDTOList"。失败时自动尝试规范化变体，避免模型措辞不当导致
# 整个工具降级到网页搜索（搜索数据精度远不如金融接口）。
_EMPTY_RESULT_SIGNS = ("无 dataTableDTOList", "接口返回中无", "dataTableDTOList")


def _query_variants(query: str) -> list[str]:
    """生成查询词的规范化变体（去重保序），最多 3 个。"""
    variants = [query]
    q2 = (query
          .replace("本周涨跌幅表现", "本周行情涨跌幅")
          .replace("涨跌幅表现", "行情涨跌幅")
          .replace("涨跌幅走势", "行情涨跌幅")
          .replace("最新走势", "行情")
          .replace("最新行情走势", "行情"))
    if q2 != query and q2 not in variants:
        variants.append(q2)
    q3 = query.replace("本周", "").replace("周度", "")
    if q3 != query and q3 not in variants:
        variants.append(q3)
    return variants[:3]


def _is_empty_result(text: str) -> bool:
    return any(sig in text for sig in _EMPTY_RESULT_SIGNS)


def _sector_query_mismatch(query: str, result_text: str) -> bool:
    """检测"问板块、但结果只有指数/全部A股汇总"的错位查询。

    东财接口对 "各板块/行业板块/概念板块 资金流向排行" 这类汇总问法不报错，
    只返回指数数据或「全部A股」合计 1 行（21:35/21:37 事故）——模型拿到
    汇总数据反复说"再查细分板块"，实际 0 个板块明细可读：
    - 场景A：问句含板块意图，返回里没有任何板块字样
    - 场景B：返回只有"全部A股"这一个聚合块（1 行合计，无个股板块明细）
    """
    _sector_intent = ("板块", "行业", "概念", "领涨", "各板", "板块排行", "板块资金")
    if not any(k in query for k in _sector_intent):
        return False
    # 场景A：返回里无板块类证券
    if "BLOCK" not in result_text and "板块" not in result_text:
        return True
    # 场景B：返回的板块只有"全部A股"汇总（单一聚合块，无多板块明细）
    if "全部A股" in result_text and "共 1 行" in result_text:
        return True
    return False


def execute(args: dict) -> str:
    """Execute a financial data query using the mx_data skill.
    查询失败（接口无法解析措辞）时自动用规范化变体重试。"""
    query = args.get("query", "")
    if not query:
        return "Error: query parameter is required"

    import multiprocessing

    if getattr(sys, "frozen", False):
        multiprocessing.freeze_support()
        for q in _query_variants(query):
            try:
                result = subprocess.run(
                    [sys.executable, "--mx-query", q],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ}
                )
                if result.returncode == 0:
                    return result.stdout.strip() or "查询完成，无输出"
                detail = result.stderr.strip() or result.stdout.strip() or "无输出"
                if _is_empty_result(detail):
                    continue  # 措辞不被接口识别 → 换变体重试
                return f"Error (exit {result.returncode}): {detail}。本工具仅支持 A股/港股/基金/板块/指数，美股等其它市场请改用 tavily_search 重试。"
            except subprocess.TimeoutExpired:
                return "Error: Query timed out (120s)"
            except Exception as e:
                return f"Error: {str(e)}"
        return "Error: 查询未返回数据（已尝试多种措辞）。本工具仅支持 A股/港股/基金/板块/指数，美股等其它市场请改用 tavily_search 重试。"

    base = Path(__file__).resolve().parent
    mx_data = None
    for _ in range(6):
        cand = base / "skills" / "mx_data" / "mx_data.py"
        if cand.exists():
            mx_data = cand
            break
        base = base.parent
    if mx_data is None or not mx_data.exists():
        return "Error: mx_data.py not found (searched from plugins/ upward)"

    for q in _query_variants(query):
        try:
            result = subprocess.run(
                [sys.executable, str(mx_data), "--query", q],
                capture_output=True, text=True, timeout=120,
                env={**os.environ}
            )
            if result.returncode == 0:
                out_text = result.stdout.strip() or "查询完成，无输出"
                # 板块类查询的后置校验：东财接口对 "各板块/行业板块/概念板块
                # 资金流向排行" 这类汇总请求只返回指数数据（不触发空错误，
                # 21:35 事故：返回的是上证/深证指数资金，没有板块数据），
                # 模型拿到后反复"读取描述文件确认板块数据"，实际无板块可读。
                # 检测到"问板块、但证券里只有指数/个股"时给明确指引。
                if _sector_query_mismatch(q, out_text):
                    out_text += (
                        "\n\n⚠️ 注意：以上返回的是指数资金数据，不含板块汇总。"
                        "东方财富接口不支持「全部板块/行业板块/概念板块资金流向排行」汇总查询。"
                        "请改为指定单个板块名称再查（如 '半导体板块资金流向'、"
                        "'人工智能板块今日涨跌幅'、'银行板块主力资金净流入'）；"
                        "若要全市场板块排名，请改用 tavily_search。"
                    )
                return out_text
            detail = result.stderr.strip() or result.stdout.strip() or "无输出"
            if _is_empty_result(detail):
                continue  # 措辞不被接口识别 → 换变体重试
            return f"Error (exit {result.returncode}): {detail}。本工具仅支持 A股/港股/基金/板块/指数，美股等其它市场请改用 tavily_search 重试。"
        except subprocess.TimeoutExpired:
            return "Error: Query timed out (120s)"
        except Exception as e:
            return f"Error: {str(e)}"
    return ("Error: 查询未返回数据（已尝试多种措辞）。本工具仅支持 A股/港股/基金/板块/指数，美股等其它市场请改用 tavily_search 重试。\n"
            "⚠️ 东方财富接口不支持「全市场/全部板块/行业板块/概念板块」这类汇总查询——请改为指定单个板块名称再查，"
            "如：'半导体板块资金流向'、'人工智能板块今日涨跌幅'、'银行板块主力资金净流入'。若用户要的是全市场板块排名，请改用 tavily_search 搜索。")


if __name__ == "__main__":
    print(execute({"query": sys.argv[1] if len(sys.argv) > 1 else "测试"}))
