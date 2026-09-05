"""Shared configuration constants (single source of truth)."""
import os
from pathlib import Path

# LLM endpoints
LM_STUDIO_URL = os.environ.get("LATIAO_LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
# 未显式选模型时的兜底名：诚实标识本地引擎（不再冒充 gpt-4o-mini——
# 09-04 用户反馈"我就没用过这个模型名"，假名导致排查误判模型来源）
SUBAGENT_MODEL = os.environ.get("LATIAO_SUBAGENT_MODEL", "latiao-local-default")

# Paths
SKILLS_DIR = Path(__file__).parent / "skills"

# Application paths
# LATIAO_TEST_PROGRESS_DIR：测试专用覆盖——假引擎场景会真实执行循环并写入
# 进度等文件，不覆盖时会把真机 ~/.local-ai-os/PROGRESS.md 越写越大。
PROGRESS_DIR = Path(os.environ.get("LATIAO_TEST_PROGRESS_DIR", "")) \
    if os.environ.get("LATIAO_TEST_PROGRESS_DIR") else Path.home() / ".local-ai-os"
