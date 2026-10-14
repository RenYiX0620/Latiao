"""反思提升的两道闸门（2026-10-12，用户拍板）：环境噪声 + 形状。

背景（真库盘点）：活跃 reflection 学习里 30 条是"环境/一次性"失败
（打错的路径 /Users/langzuxiax/、把 URL 当路径、二进制当文本读、应用自身回读校验、
瞬时 ConnectionError），27 条是**逐字抄来的聊天/文档片段**（含一条私密角色扮演台词、
10 条建筑工程合同句子）。两者都不是知识，却会被语义检索注入。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "reflectgate-token")

from memory import (  # noqa: E402
    _reflection_is_env_noise,
    _reflection_is_known_template,
)


def test_env_noise_is_filtered():
    """环境/一次性失败：真机原句全部拦下。"""
    real = [
        ("工具 read_file 失败：错误：文件不存在 - /Users/langzuxiax/A",
         "read_file(path=/Users/langzuxiax/Agent/local-ai-os/SOUL.md) 失败：工具 read_file 执行出错"),
        ("工具 read_file 失败：错误：文件不存在 - https://claudemarke",
         "read_file(path=https://claudemarketplaces.com/skills/mi) 失败：工具 read_file 执行出错"),
        ("工具 write_file 失败：failed: could not read back fi",
         "write_file(content=# 报告) 失败：工具 write_file 执行出错"),
        ("工具 run_cmd 失败：错误：'utf-8' codec can't decode",
         "run_cmd(cmd=sqlite3 /Users/langzuxiang/.local-ai-os/) 失败：工具 run_cmd 执行出错"),
        ("工具 ak_finance 失败：Error: 金融数据查询失败: ConnectionErr",
         "ak_finance(period=daily) 失败：工具 ak_finance 执行出错，可能需要重试或调整参数"
         "（Error: 金融数据查询失败: ConnectionError: ('Connection abor"),
        ("工具 list_dir 失败：错误：[Errno 2] No such file or d",
         "list_dir(path=/Users/langzuxiang/skills) 失败：工具 list_dir 执行出错"),
    ]
    for topic, content in real:
        assert _reflection_is_env_noise(topic, content) is True, topic


def test_reusable_pitfalls_are_kept():
    """反控：可复用的坑（参数用法 / 未知工具）不得误伤。"""
    keep = [
        ("工具 search_files 失败：Error: Missing required argume",
         "search_files(path=…) 失败：工具 search_files 执行出错｜特征：Error: Missing required argument"),
        ("工具 curl 失败：Error: Unknown tool 'curl'",
         "curl(output_path=…) 失败：工具 curl 执行出错｜特征：Error: Unknown tool 'curl'"),
        ("工具 mx_query 失败：查询多指数时需指定具体字段",
         "mx_query(query=…) 失败：该接口不支持板块汇总查询，需指定单个板块名"),
    ]
    for topic, content in keep:
        assert _reflection_is_env_noise(topic, content) is False, topic


def test_shape_gate_rejects_verbatim_chat_and_documents():
    """形状闸门：非应用模板的反思（逐字聊天/文档片段）拦下。"""
    junk = [
        "发现你的小骚逼粉嫩粉嫩的啊",
        "发现总承包计价清单与招标项目的建设规模、建设",
        "发现文物后不及时报告或隐瞒不报，致使文物丢失或",
        "错误：未安装",
        'error":"Unauthorized"}',
        "Traceback (most recent call last",
    ]
    for r in junk:
        assert _reflection_is_known_template(r) is False, r


def test_shape_gate_accepts_the_four_app_templates():
    """应用自己的四类模板句必须放行（否则会把整条反思学习链掐死）。"""
    ok = [
        "工具 read_file 执行出错，可能需要重试或调整参数（错误：…）",
        "权限不足，建议检查文件/目录权限（Permission denied）",
        "目标不存在，可能需要先确认路径或创建前置资源",
        "工具返回为空，可能参数不正确或目标无内容",
    ]
    for r in ok:
        assert _reflection_is_known_template(r) is True, r


def test_promote_refuses_junk_without_writing(tmp_path, monkeypatch):
    """集成：提升入口对两类噪声都返回 False（且不留库）。"""
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path))

    import memory as M

    # 记录调用而不是抛异常：promote 内部对 _store_learning 有 try/except Exception，
    # 抛哨兵会被吞掉（首版正控就是这么假绿的——函数返回 False 但原因看不出来）。
    seen: list = []
    monkeypatch.setattr(M, "_store_learning", lambda *a, **kw: seen.append(a))
    # 环境噪声：闸门拦下，永不写库
    assert M.promote_reflection_to_learning(
        "s", "read_file", {"path": "/nope"},
        "错误：文件不存在 - /nope", "工具 read_file 执行出错，可能需要重试或调整参数") is False
    # 形状不符：同样拦下
    assert M.promote_reflection_to_learning(
        "s", "read_file", {"path": "/x"},
        "Error: 未安装", "发现你的小骚逼粉嫩粉嫩的啊") is False
    assert seen == [], "被闸门拦下的条目不得触达 _store_learning"
    # 正控：模板句 + 可复用特征（参数用法）→ 必须真的写入
    assert M.promote_reflection_to_learning(
        "s", "search_files", {"path": "/x"},
        "Error: Missing required argument", "工具 search_files 执行出错，可能需要重试或调整参数") is True
    assert len(seen) == 1, "可复用的坑必须走到写库（闸门误伤会让整条反思学习链失效）"
