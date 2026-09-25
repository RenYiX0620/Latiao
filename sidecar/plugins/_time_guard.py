#!/usr/bin/env python3
"""_time_guard - 搜索词日期滑差自检（共享辅助，非工具插件）。

模型把"昨晚/今天"等相对时间换算错后写进搜索词（09-03 两次事故：8:25 老会话、
8:57 全新会话，均把"昨晚美股"搜成 9月1日），检索回旧日期数据后被锚定。
本模块在搜索结果里补一行警告，促使模型核对换算、必要时重搜。
"""

import re
from datetime import datetime, timedelta

# 2026年9月1日 / 2026-09-01 / 2026/9/1 / 9月1日（无年份）
_DATE_PATTERNS = [
    re.compile(r"(20\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})\s*日?"),
    re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]


def check_query_date(query: str, now: datetime | None = None) -> str | None:
    """检查搜索词中的明确日期是否早于当前日期 ≥2 天（前天及更早）。

    阈值取 2 天而非 1 天："昨晚"的正确换算就是昨天（今天 9月3日 搜 9月2日 是
    对的）——若对昨天也警告，模型会被误导成循环重搜（重放实测 35 次同询）。
    实际故障特征是多减一天锚到前天（9月3日 搜 9月1日），≥2 天恰好命中。
    返回警告行文本（需拼进搜索结果尾部）或 None（不警告）。
    now 参数供测试注入。
    """
    if not query:
        return None
    now = now or datetime.now()
    found = None
    for pat in _DATE_PATTERNS:
        m = pat.search(query)
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 3:
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
        else:
            # 无年份：按当前年；若得到的日期在未来超过 1 天（跨年查 12 月），视为去年
            month, day = int(groups[0]), int(groups[1])
            year = now.year
            try:
                candidate = datetime(year, month, day)
                if (candidate - now) > timedelta(days=1):
                    year -= 1
            except ValueError:
                return None
        try:
            found = datetime(year, month, day)
        except ValueError:
            return None
        break
    if found is None or (now - found) < timedelta(days=2):
        return None
    return (
        f"⚠️ 日期核对：搜索词中的日期 {found.strftime('%Y-%m-%d')} 早于今天 "
        f"{now.strftime('%Y-%m-%d')} 两天以上，很可能是相对时间换算时多减了天数"
        f"（例如把“昨晚”错算成前天）。请按当前时间重新换算——注意：美股“昨晚”"
        f"指北京时间今天凌晨收盘的那一场——换算错误时用正确日期重新搜索；"
        f"若用户确实在查历史日期，可忽略本警告。"
    )
