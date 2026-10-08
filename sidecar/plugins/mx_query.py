from cmd_safety import child_env   # ④ 子进程 env 白名单
#!/usr/bin/env python3
"""mx_query - 妙想金融数据查询工具"""
import re
import subprocess
import sys
from pathlib import Path

NAME = "mx_query"
PERMISSION = "safe"
DEFINITION = {
    "type": "function",
    "function": {
        "name": "mx_query",
        "description": "查询【中国A股、港股、基金、板块、行业、指数】的实时行情、财务数据、资金流向等金融数据的专用工具，基于东方财富权威数据库。仅支持境内市场。⚠️适用判断：仅当用户询问的对象是 A股/港股/基金/板块/指数 时使用本工具（如：贵州茅台股价、A股大盘走势、恒生指数、某基金净值）。主力资金流向/概念板块/多市场对比优先用本工具（ak_finance 不提供这些）。⚠️不支持的市场：美股、纳斯达克、道琼斯、标普、外汇、加密货币等一切境外/非证券市场——美股指数日线可改用 ak_finance，其余境外问题请直接改用 tavily_search，不要调用本工具，否则必然返回空数据。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "金融数据查询语句。已验证可用的措辞（务必照此模式，其他措辞常返回空）：个股行情/财务=\"贵州茅台 股价\"或\"贵州茅台 2024 财务数据\"；指数K线=\"上证指数 近5个交易日 日线 收盘价 成交量\"；均线=\"上证指数 5日均线 10日均线\"；板块=\"半导体板块 今日涨跌幅 资金流向\"；资金=\"上证指数 主力资金净流入 今日\"。✅本工具**支持一次查多个标的**（实测有效，返回一张多列表格，比逐个查更快更省）：指数=\"上证指数,深证成指,创业板指,科创50,北证50 涨跌幅 主力净流入\"；板块=\"半导体,人工智能,新能源板块 涨跌幅 资金流向\"。一次查询能带上的标的就一次带上，不要逐个调用。⚠️返回通常含两段：『当前』快照行 与 最近若干『日线』行——两者是不同口径，引用时必须写明用的是哪一段。⚠️\"全部A股(板块)涨跌排行\"类汇总查询无明细数据，不要用——板块排行请改用 tavily_search。"
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


def _mx_child_env():
    """子进程 env：白名单 + 显式注入妙想 key（它已不在 sidecar 的环境里）。"""
    try:
        from runtime_secrets import get as _secret_get
        key = _secret_get("MX_APIKEY")
    except Exception:
        key = ""
    return child_env(extra={"MX_APIKEY": key} if key else None,
                     allow_prefixes=("MX_",))


# ── 名称核对（2026-09-30）：空结果时告诉模型"名字不对"，而不是让它换同义名 ──
# 事故：问"超节点/超算中心 涨跌幅 资金流向" → 两个名字都不在东财标的库（正式板块是
# 「算力概念」「液冷服务器」）→ 模型在同义名之间打转。这里用东财**公开搜索建议**接口
# 核对一次名称（匿名可用、不吃妙想配额；失败即降级，绝不因此吞掉原始报错）。
_SUGGEST_URL = ("https://searchapi.eastmoney.com/api/suggest/get"
                "?input={kw}&type=14&count=8")
_STRIP_WORDS = ("涨跌幅", "涨跌", "资金流向", "资金", "主力", "净流入", "净流出",
                "今日", "今天", "昨天", "本周", "本月", "行情", "走势", "最新",
                "板块", "概念", "行业", "数据", "查询", "的", "了", "请")
_SUMMARY_SIGNS = ("全部", "所有", "各个", "各板", "全市场", "排行", "排名", "汇总", "一览")


def _name_candidates(query: str) -> list[str]:
    """从查询里挑出"可能是标的/板块名"的词（去掉意图词与标点），最多 2 个。"""
    q = str(query or "")
    for w in _STRIP_WORDS:
        q = q.replace(w, " ")
    out: list[str] = []
    for tok in re.split(r"[\s、，,;；/]+", q):
        tok = tok.strip("：:。.!！?？\"'")
        if len(tok) >= 2 and tok not in out:
            out.append(tok)
    return out[:2]


def _suggest_names(kw: str) -> list[tuple[str, str]]:
    """东财搜索建议 → [(名称, 代码)]（板块优先）。任何失败都返回空表（降级）。"""
    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur
    try:
        url = _SUGGEST_URL.format(kw=_up.quote(kw))
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=5) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
        items = ((data.get("QuotationCodeTable") or {}).get("Data")) or []
        hits = [(str(i.get("Name") or ""), str(i.get("Code") or "")) for i in items
                if i.get("Name")]
        # 板块（BK 开头）排前面：模型要的就是它
        hits.sort(key=lambda x: (not x[1].startswith("BK"),))
        return hits[:4]
    except Exception:
        return []


def _name_check_hint(query: str) -> str:
    """空结果时的名称核对说明（挑不出名字或接口失败时返回空串，由调用方走原兜底）。"""
    cands = _name_candidates(query)
    if not cands:
        return ""
    lines: list[str] = []
    found = False
    for kw in cands:
        hits = _suggest_names(kw)
        if not hits:
            lines.append(f"· 「{kw}」不在东财标的库中（板块/个股/基金/指数都没有这个名称）。")
            continue
        found = True
        show = "、".join(f"{n}({c})" if c else n for n, c in hits)
        lines.append(f"· 「{kw}」→ 相近的正式名称：{show}")
    if not lines:
        return ""
    head = "\n【名称核对】"
    tail = ("\n请**用上面的正式名称重新查询**（板块名可直接带「板块」二字），或改用 tavily_search "
            "搜该题材新闻（并在回答里注明：该题材没有板块级行情/资金数据）；"
            "也可以改查该题材的产业链个股。" if found else
            "\n这类题材/口语名在东财体系里没有对应板块——请改用相近的正式板块名、"
            "或查该题材的产业链个股、或用 tavily_search 搜新闻并注明无板块级数据。"
            "**不要**继续换同义名重试。")
    return head + "\n".join(lines) + tail


def _is_summary_query(query: str) -> bool:
    return any(k in str(query or "") for k in _SUMMARY_SIGNS)


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
                    env=_mx_child_env()   # ④ 白名单：只额外带上数据源自己的凭据
                )
                if result.returncode == 0:
                    return result.stdout.strip() or "查询完成，无输出"
                detail = result.stderr.strip() or result.stdout.strip() or "无输出"
                if _is_empty_result(detail):
                    continue  # 措辞不被接口识别 → 换变体重试
                if "113" in detail or "上限" in detail or "达到" in detail:
                    return ("Error: mx_query 免费版当日调用次数已用完（150 次/日）。"
                            "⚠️ 重试本工具无效，当日不会再成功。请立即改用："
                            "ak_finance（免费行情，指数/个股可用）或 tavily_search（联网搜索板块排行/新闻）。")
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
                env=_mx_child_env()   # ④ 白名单：只额外带上数据源自己的凭据
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
            if "113" in detail or "上限" in detail or "达到" in detail:
                return ("Error: mx_query 免费版当日调用次数已用完（150 次/日）。"
                        "⚠️ 重试本工具无效，当日不会再成功。请立即改用："
                        "ak_finance（免费行情，指数/个股可用）或 tavily_search（联网搜索板块排行/新闻）。")
            return f"Error (exit {result.returncode}): {detail}。本工具仅支持 A股/港股/基金/板块/指数，美股等其它市场请改用 tavily_search 重试。"
        except subprocess.TimeoutExpired:
            return "Error: Query timed out (120s)"
        except Exception as e:
            return f"Error: {str(e)}"
    _base = ("Error: 查询未返回数据（已尝试多种措辞）。本工具仅支持 A股/港股/基金/板块/指数，"
             "美股等其它市场请改用 tavily_search 重试。")
    if _is_summary_query(query):
        return (_base + "\n⚠️ 东方财富接口不支持「全市场/全部板块/行业板块/概念板块」这类汇总查询——"
                "请改为指定单个板块名称再查，如：'半导体板块资金流向'、'人工智能板块今日涨跌幅'、"
                "'银行板块主力资金净流入'。若用户要的是全市场板块排名，请改用 tavily_search 搜索。")
    # 非汇总查询：多半是**名字不对**。核对一次名称，把"该换什么"直接告诉模型
    # （此前这里套的是"汇总查询"提示，把模型引向"换个板块名再试" → 同义名打转）
    try:
        _hint = _name_check_hint(query)
    except Exception:      # 核对本身出任何问题都不许顶掉"查询未返回数据"这个原始事实
        _hint = ""
    return _base + (_hint or "\n提示：若查的是题材/口语名（东财没有该正式板块），请改用相近的"
                             "正式板块名、或该题材的产业链个股，或用 tavily_search 搜新闻。")


if __name__ == "__main__":
    print(execute({"query": sys.argv[1] if len(sys.argv) > 1 else "测试"}))
