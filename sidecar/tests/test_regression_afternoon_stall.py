"""今天下午"任务半途停"三形态修复（2026-10-20）。

A 催促不升温 → 温度 0 + 缓存 100% 四连复读同一句承诺
B 收口轮保留 tools → 工具形态锁定，正文永远是废话
C 实质门槛太松 → 326 字半截分析被判完整；兜底路径无日志
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "stall-token")

SRC = Path(__file__).resolve().parents[1].joinpath("agent", "loop.py").read_text("utf-8")


def test_plan_nudge_bumps_temperature():
    """催促轮必须升温破确定性复读（14:44 四连逐字节相同）。"""
    idx = SRC.find("plan_nudge_used += 1")
    seg = SRC[idx:idx + 800]
    assert "_retry_temp" in seg and "0.6" in seg, "催促分支必须设 _retry_temp=0.6"
    assert "升温破复读" in seg


def test_finalize_clears_tools():
    """收口轮必须 pop tools（17:17 工具形态锁定）。"""
    # 锚定 _build_request 里的 finalize 段（不是 _build_finalize_digest 里的同名调用）
    idx = SRC.find("def _build_request")
    idx = SRC.find("if self._finalize_round:", idx)
    seg = SRC[idx:idx + 1200]
    assert 'body.pop("tools", None)' in seg
    assert "self.native_tools = False" in seg


def test_substance_threshold_tightened():
    """实质门槛：≥3 数据事实（短而数据密的直答仍合法）；% 需 ≥2 事实。"""
    idx = SRC.find("_has_substance = (")
    seg = SRC[idx:idx + 500]
    assert "_fact_n >= 3" in seg
    assert "_fact_n >= 2" in seg
    assert ">= 80" in seg


def test_fallback_path_logs():
    """第二次 junk 的数据兜底必须记日志（此前盲区）。"""
    idx = SRC.find("_collect_finalize_data(self.current_msgs, max_results=8)")
    seg = SRC[idx:idx + 700]
    assert "数据兜底交付" in seg
    assert "无可用数据" in seg


def test_prose_data_fact_count_strips_dates():
    """日期/列表编号不算数据事实（19:30 漏网根因之一）。"""
    from agent.loop import _prose_data_fact_count
    # 纯日期 + 列表编号 → 0 个数据事实
    assert _prose_data_fact_count("1. 查找 2026-10-20 的数据\n2. 10月20日 19:30") == 0
    # 真价格/百分比 → 算
    assert _prose_data_fact_count("上证 3856.12 点，涨 1.66%") >= 2
