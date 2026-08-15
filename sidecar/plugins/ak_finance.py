"""ak_finance - 免费金融数据查询（基于 AKShare，无需 API Key）
覆盖 A股/港股/指数/基金/板块，数据源为东方财富等公开数据。"""
import asyncio
import re

# 东方财富风控会拒绝 python-requests 默认 UA → 全局打浏览器 UA 补丁
import requests.utils

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_orig_default_headers = requests.utils.default_headers


def _patched_default_headers():
    h = _orig_default_headers()
    h["User-Agent"] = _BROWSER_UA
    return h


requests.utils.default_headers = _patched_default_headers

# 东财部分数字子域（如 48.push2）会被服务端拒绝连接 → 统一重写为无前缀域名
_ORIG_REQ_GET = requests.get
_ORIG_REQ_POST = requests.post


def _fix_url(url):
    if isinstance(url, str):
        url = re.sub(r"https?://\d+\.push2\.eastmoney\.com", "https://push2.eastmoney.com", url)
        url = re.sub(r"https?://\d+\.push2his\.eastmoney\.com", "https://push2his.eastmoney.com", url)
    return url


def _fixed_get(url, *args, **kwargs):
    return _ORIG_REQ_GET(_fix_url(url), *args, **kwargs)


def _fixed_post(url, *args, **kwargs):
    return _ORIG_REQ_POST(_fix_url(url), *args, **kwargs)


requests.get = _fixed_get
requests.post = _fixed_post

NAME = "ak_finance"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "ak_finance",
        "description": "免费金融数据查询（无需 API Key，基于 AKShare/东方财富公开数据）。支持【A股、港股、指数、基金、行业板块】的实时行情与历史数据。适用：问任何 A股/港股个股价格、上证/深证/恒生等指数、基金净值、板块行情。不适用：美股、加密货币等境外市场（请用网页搜索）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "金融查询语句，如：贵州茅台股价、上证指数、恒生指数、000001、招商中证白酒基金净值、半导体板块"
                },
                "period": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "description": "历史数据周期（仅查询历史行情时用）。默认 daily。"
                },
            },
            "required": ["query"],
        },
    },
}


def _fmt_num(v) -> str:
    """格式化数值，None/NaN → '-'"""
    if v is None:
        return "-"
    try:
        f = float(v)
        if f != f:  # NaN
            return "-"
        if abs(f) >= 1e8:
            return f"{f/1e8:.2f}亿"
        if abs(f) >= 1e4:
            return f"{f/1e4:.2f}万"
        return f"{f:.2f}"
    except (TypeError, ValueError):
        return str(v)


def _em_get(url: str, params: dict | None = None, retries: int = 3) -> "requests.Response":
    """东财接口请求：带重试 + 递增间隔，缓解频控导致的 RemoteDisconnected"""
    import time as _time

    import requests as _req
    last_err = None
    for i in range(retries):
        try:
            r = _req.get(url, params=params, timeout=15)
            if r.status_code == 200 and r.text.strip() and not r.text.strip().startswith("<"):
                return r
            last_err = f"HTTP {r.status_code} / 空响应"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        _time.sleep(1.2 * (i + 1))
    raise ConnectionError(f"东财接口请求失败: {url} ({last_err})")


def _fmt_row(row, name_col, price_col, chg_col=None, extra=None) -> str:
    name = str(row.get(name_col, "?"))
    price = _fmt_num(row.get(price_col))
    parts = [f"{name}: {price}"]
    if chg_col and chg_col in row:
        chg = row.get(chg_col)
        if chg is not None:
            parts.append(f"涨跌幅 {_fmt_num(chg)}%")
    if extra:
        for k in extra:
            if k in row and row.get(k) is not None:
                parts.append(f"{k} {_fmt_num(row[k])}")
    return "  ".join(parts)


_TX_INDEX_MAP = {
    "上证": "sh000001", "深证成指": "sz399001", "创业板指": "sz399006",
    "沪深300": "sh000300", "科创50": "sh000688", "中证500": "sh000905",
    "中证1000": "sh000852", "恒生": "hkHSI", "恒指": "hkHSI",
    "上证50": "sh000016", "科创100": "sh000698",
}


def _parse_tx_quote(text: str) -> dict | None:
    """解析腾讯行情 v_sh600519=\"1~名称~代码~现价~昨收~今开~...~时间~涨跌~涨跌幅%...\" """
    m = re.search(r'="(.*)"', text)
    if not m:
        return None
    f = m.group(1).split("~")
    if len(f) < 35:
        return None
    return {
        "name": f[1], "code": f[2], "price": f[3], "prev": f[4], "open": f[5],
        "time": f[30], "chg": f[31], "pct": f[32], "high": f[33], "low": f[34],
    }


async def _tx_quote(codes: list[str]) -> str:
    """批量查询腾讯行情"""
    import requests as _req

    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    try:
        r = await asyncio.to_thread(_req.get, url, timeout=15)
        if r.status_code != 200 or not r.text.strip():
            return ""
    except Exception:
        return ""
    lines = []
    for line in r.text.splitlines():
        q = _parse_tx_quote(line)
        if not q:
            continue
        lines.append(f"  {q['name']}({q['code']}): {_fmt_num(q['price'])}  "
                     f"涨跌幅 {_fmt_num(q['pct'])}%  今开 {_fmt_num(q['open'])}  "
                     f"最高 {_fmt_num(q['high'])} 最低 {_fmt_num(q['low'])}  "
                     f"时间 {q['time']}")
    return "\n".join(lines)


async def _query_a_share_spot(query: str) -> str:
    """A股实时行情：腾讯 suggest 搜索 + 行情接口（免 key、秒级、无限流）"""
    import requests as _req

    kw = query.strip()
    for w in ("股价", "行情", "最新价", "今天", "今日", "走势", "多少钱", "价格", "收盘", "实时"):
        kw = kw.replace(w, "")
    kw = kw.strip()
    if not kw:
        return f"未找到 A股: {query}"

    # 名称 → 市场代码 (如 sh600519)
    codes = []
    m = re.search(r"\b(\d{6})\b", kw)
    if m:
        # 6 位代码：尝试沪/深前缀
        codes = [f"sh{m.group(1)}", f"sz{m.group(1)}"]
    else:
        try:
            r = await asyncio.to_thread(_req.get,
                "https://smartbox.gtimg.cn/s3/", params={"v": "2", "q": kw, "t": "all"}, timeout=15)
            hits = re.findall(r'"(\w{2})~(\d{6})~[^~]+~[^~]+~GP-A"', r.text)
            codes = [f"{mkt}{code}" for mkt, code in hits[:5]]
        except Exception as e:
            return f"Error: 股票搜索失败: {e}"
    if not codes:
        return f"未找到 A股: {query}（请确认股票名称或 6 位代码）"

    body = await _tx_quote(codes)
    if not body:
        return f"未找到 A股: {query}"
    return "📈 A股实时行情:\n" + body


async def _query_index(query: str) -> str:
    """指数行情：腾讯接口（免 key、稳定），关键词映射 + suggest 兜底"""
    code = None
    for key, c in _TX_INDEX_MAP.items():
        if key in query:
            code = c
            break
    if code:
        body = await _tx_quote([code])
        if body:
            return "📊 指数行情:\n" + body
    # 兜底：suggest 搜索指数
    try:
        import requests as _req
        r = await asyncio.to_thread(_req.get,
            "https://smartbox.gtimg.cn/s3/", params={"v": "2", "q": query, "t": "all"}, timeout=15)
        hits = re.findall(r'"(\w{2})~(\d{5,6})~[^~]+~[^~]+~GP-A"', r.text)
        codes = [f"{mkt}{c}" for mkt, c in hits[:5]]
        if codes:
            body = await _tx_quote(codes)
            if body:
                return "📊 指数行情:\n" + body
    except Exception:
        pass
    return f"未找到指数: {query}"


async def _query_fund(query: str) -> str:
    """基金：按名称/代码模糊匹配，返回净值"""
    import akshare as ak

    df = await asyncio.to_thread(ak.fund_open_fund_rank_em)
    code = None
    m = re.search(r"\b(\d{6})\b", query)
    if m:
        code = m.group(1)
    name_kw = query.replace("基金", "").replace("净值", "").strip()
    rows = None
    if code:
        rows = df[df["基金代码"] == code].head(3)
    if rows is None or len(rows) == 0:
        rows = df[df["基金简称"].str.contains(name_kw, na=False)].head(5)
    if rows is None or len(rows) == 0:
        return f"未找到基金: {query}"

    lines = ["💰 基金净值:"]
    for _, r in rows.iterrows():
        lines.append("  " + _fmt_row(r, "基金简称", "单位净值", "日增长率",
                                     ["基金代码", "累计净值", "日期"]))
    return "\n".join(lines)


async def _query_board(query: str) -> str:
    """行业板块行情"""
    import akshare as ak

    df = await asyncio.to_thread(ak.stock_board_industry_name_em)
    kw = query.replace("板块", "").replace("行业", "").strip()
    hit = df[df["板块名称"].str.contains(kw, na=False)].head(8)
    if len(hit) == 0:
        return f"未找到板块: {query}"
    lines = ["🏷️ 行业板块行情:"]
    for _, r in hit.iterrows():
        lines.append("  " + _fmt_row(r, "板块名称", "最新价", "涨跌幅",
                                     ["总市值", "上涨家数", "下跌家数"]))
    return "\n".join(lines)


async def _query_a_share_hist(query: str) -> str:
    """个股历史行情（含代码 + 历史/走势 关键词时）"""
    import akshare as ak

    m = re.search(r"\b(\d{6})\b", query)
    if not m:
        return ""
    code = m.group(1)
    df = await asyncio.to_thread(
        ak.stock_zh_a_hist, symbol=code, period="daily",
        start_date="20250101", adjust="qfq")
    if df is None or len(df) == 0:
        return ""
    recent = df.tail(10)
    lines = [f"📅 {code} 近期日线行情 (前复权):"]
    for _, r in recent.iterrows():
        lines.append(f"  {r['日期']}: 开{_fmt_num(r['开盘'])} 高{_fmt_num(r['最高'])} "
                     f"低{_fmt_num(r['最低'])} 收{_fmt_num(r['收盘'])} 涨跌幅 {_fmt_num(r['涨跌幅'])}%")
    return "\n".join(lines)


async def execute(args: dict) -> str:
    """按 query 智能路由到对应 akshare 接口"""
    query = (args.get("query", "") or "").strip()
    if not query:
        return "Error: query 参数必填"
    try:
        # 1) 历史行情：含 6 位代码 + 历史/走势关键词
        if re.search(r"历史|走势|近期|k线|K线|日线", query) and re.search(r"\b\d{6}\b", query):
            r = await _query_a_share_hist(query)
            if r:
                return r
        # 2) 指数
        if any(k in query for k in ("指数", "大盘", "上证", "深证", "创业板", "科创", "恒生", "沪深")):
            r = await _query_index(query)
            if "未找到" not in r:
                return r
        # 3) 基金
        if "基金" in query or "净值" in query:
            r = await _query_fund(query)
            if "未找到" not in r:
                return r
        # 4) 板块
        if "板块" in query or "行业" in query:
            r = await _query_board(query)
            if "未找到" not in r:
                return r
        # 5) A股个股
        return await _query_a_share_spot(query)
    except Exception as e:
        return f"Error: 金融数据查询失败: {type(e).__name__}: {e}"


if __name__ == "__main__":
    import sys
    asyncio.run(execute({"query": " ".join(sys.argv[1:])}))
