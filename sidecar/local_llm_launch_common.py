"""启动辅助：GGUF 定位、chat template 猜测、llama-server 二进制定位。

从 local_llm_engine.EngineProcess 拆出（2026-09-24）：纯函数，engine 作首参（如需要）。"""

from __future__ import annotations

from pathlib import Path

import logging
logger = logging.getLogger("latiao-sidecar")

_ENGINE_ENV_PREFIXES = ("DYLD_", "LD_", "MTL_", "GGML_", "OMP_", "CUDA_", "VK_", "HF_", "MLX_")


def guess_chat_format(model_path: str) -> str | None:
    """Guess llama-cpp chat format from model path for proper function calling.
    Covers tool-calling models: Hermes-2-Pro, Qwen2.5+, Functionary, Gemma, Llama3, Mistral."""
    lower = model_path.lower()
    if "hermes" in lower and ("pro" in lower or "2-pro" in lower):
        return "hermes-2-pro"
    if "functionary" in lower:
        return "functionary"
    if "qwen" in lower:
        if any(v in lower for v in ["2.5", "3.", "qwen3"]):
            return "qwen"  # Qwen 2.5+ has native tool calling
        return "qwen"
    if "gemma" in lower:
        # Gemma models have proper chat templates in GGUF metadata.
        # Don't override with llama.cpp's default Gemma format
        # (which doesn't support system role).
        return None
    if "phi-4" in lower or "phi4" in lower:
        return "phi"
    if "llama-4" in lower or "llama4" in lower:
        return "llama3"  # Llama 4 supports tool calling
    if "llama-3.2" in lower or "llama-3.1" in lower or "llama-3" in lower:
        return "llama3"
    if "llama" in lower:
        return "llama3"  # Default for newer Llamas
    if "mistral" in lower or "mixtral" in lower:
        return "mistral-instruct"
    if "command-r" in lower or "c4ai" in lower:
        return "command-r"
    if "deepseek" in lower and "v3" in lower:
        return "deepseek"
    return None



def find_llama_server(model_path: str = "") -> Path | None:
    """定位原生 llama-server（双引擎分派，09-15）。

    - macOS: sidecar/llama-server            = XHToken fork（Spark-X2.5 新架构补丁）
             sidecar/llama-upstream/llama-server = 上游最新（普通模型兼容性更好）
    - Windows: sidecar/llama-server.exe（CI 下载的上游构建）

    Spark 类模型必须用 fork（上游不认它的模板/函数调用格式）；其余优先上游，
    上游缺失时回退 fork（保持旧行为，不至于因缺文件而无法启动）。
    """
    base = Path(__file__).parent
    is_spark = "spark" in (model_path or "").lower()
    if is_spark:
        # fork 优先；缺失时仍试上游与 exe（宁可尝试也不硬失败——
        # 09-15 事故：CI 包缺 fork 且直接报 python 的 ValueError，
        # 用户只看到 "Failed to load model" 一头雾水）
        cands = [base / "llama-server", base / "llama-upstream" / "llama-server",
                 base / "llama-server.exe"]
    else:
        cands = [base / "llama-upstream" / "llama-server",
                 base / "llama-server.exe",
                 base / "llama-server"]
    for exe in cands:
        try:
            if exe.exists() and exe.is_file():
                if "llama-upstream" in str(exe):
                    logger.info("原生引擎分派: 上游 llama.cpp（%s）", exe.parent.name)
                return exe
        except OSError:
            continue
    return None


def find_mmproj_for(model_path: str) -> Path | None:
    """找模型配套的多模态投影器（识图）：mmproj*.gguf。

    查找顺序：模型同目录 → 上级目录 → ~/Models 顶层。Hermes/Qwen-VL 这类
    GGUF 多模态要把 mmproj 和主模型放在同一目录，启动时自动挂上。
    """
    if not model_path:
        return None
    p = Path(model_path)
    dirs: list[Path] = []
    if p.is_file():
        dirs.append(p.parent)
    elif p.is_dir():
        dirs.append(p)
        dirs.append(p.parent)
    try:
        from local_llm_probe import MODELS_DIR
        dirs.append(Path(MODELS_DIR))
    except Exception:
        pass
    seen: set[str] = set()
    for d in dirs:
        try:
            if not d or not d.exists():
                continue
            key = str(d.resolve())
            if key in seen:
                continue
            seen.add(key)
            hits = sorted(d.glob("mmproj*.gguf")) + sorted(d.glob("mmproj*.bin"))
            if hits:
                logger.info("找到 mmproj 投影器: %s", hits[0])
                return hits[0]
        except OSError:
            continue
    return None
