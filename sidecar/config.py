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
PROGRESS_DIR = Path.home() / ".local-ai-os"
