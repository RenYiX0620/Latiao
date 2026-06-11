"""Shared configuration constants (single source of truth)."""
import os
from pathlib import Path

# LLM endpoints
LM_STUDIO_URL = os.environ.get("LATIAO_LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
SUBAGENT_MODEL = os.environ.get("LATIAO_SUBAGENT_MODEL", "gpt-4o-mini")

# Paths
SKILLS_DIR = Path(__file__).parent / "skills"

# Application paths
PROGRESS_DIR = Path.home() / ".local-ai-os"
