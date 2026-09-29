"""测试共享配置（conftest）。

关键：场景测试会真实执行 agent 循环并写入进度/DB/memory 等文件——
pytest_configure 在收集/导入任何测试模块之前把 PROGRESS_DIR 重定向到
临时目录（config.py 支持 LATIAO_TEST_PROGRESS_DIR 覆盖），避免污染真机
~/.local-ai-os（此前 PROGRESS.md 被场景测试越写越大，见阶段 2b 基线提交）。
"""
import os
import tempfile


def pytest_configure(config):
    if not os.environ.get("LATIAO_TEST_PROGRESS_DIR"):
        os.environ["LATIAO_TEST_PROGRESS_DIR"] = tempfile.mkdtemp(
            prefix="latiao-test-progress-")
