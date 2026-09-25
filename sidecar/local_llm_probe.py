"""本地模型探测（GGUF 扫描 / MLX 架构 / 自定义引擎配置）— 纯函数。

从 local_llm.py 拆出（2026-09-24）。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import ssl
from pathlib import Path

import certifi

logger = logging.getLogger("latiao-sidecar")

_ENGINE_ENV_PREFIXES = ("DYLD_", "LD_", "MTL_", "GGML_", "OMP_", "CUDA_", "VK_", "HF_", "MLX_")

MODELS_DIR = Path(os.environ.get("LATIAO_MODELS_DIR", Path.home() / "Models"))
MODELS_DIR.mkdir(parents=True, exist_ok=True)

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"
IS_APPLE_SILICON = IS_MAC and (platform.processor() == "arm" or "Apple" in platform.processor())

try:
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _ssl_ctx = ssl.create_default_context()

def _fix_tokenizer_alias(model_dir) -> None:
    """修复 HF 下载重名产物：tokenizer.json 缺失但有 tokenizer-2.json 时自动补。

    tokenizer.json 缺失 → tokenizer 退化 → 模型生成的 token 无法解码成文本
    → "全空输出"（09-04 Ornith-4bit 事故：生成 60 token 但 content 恒空，
    触发就绪探测失败/agent 判定无实质内容/任务中止的整条链路）。
    """
    try:
        model_dir = Path(model_dir)
        if not (model_dir / "tokenizer.json").exists() and (model_dir / "tokenizer-2.json").exists():
            (model_dir / "tokenizer-2.json").rename(model_dir / "tokenizer.json")
            logger.info("tokenizer.json 缺失，已从 tokenizer-2.json 补齐: %s", model_dir.name)
    except OSError:
        pass


def _detect_model_bits(model_path: str) -> int:
    """Detect model quantization bits from filename. Returns 4, 5, 6, 8, or 16."""
    import re
    name = Path(model_path).name.upper().replace(".GGUF", "")
    # Match common quantization markers: Q4_K_M, Q5_0, IQ3_XXS, Q8_0, etc.
    m = re.search(r'(?:^|[._-])(?:Q|IQ)(\d)', name)
    if m:
        return int(m.group(1))
    m = re.search(r'(?:^|[._-])(F16|FP16|F32|FP32)', name)
    if m:
        return 16
    # Default: assume 4-bit (most common download)
    return 4


def _resolve_mlx_path(model_id: str, models_dir: Path = MODELS_DIR, hf_hub: Path | None = None) -> str:
    """把模型 id 解析成 mlx_lm 可加载的本地 MLX 目录/路径。

    优先级：直接路径 → ~/Models 下的 MLX 目录（含 config.json + 权重）
    → LM Studio / Ollama 管理器目录 → HF hub 缓存 snapshots
    → 原样返回（当作 HF repo id，由 mlx_lm 下载）。
    MLX 模型是目录结构（config.json + model.safetensors/weights.npz），
    没有通用的"单个 .mlx 文件"，因此不能只按文件名匹配。
    """
    try:
        p = Path(model_id)
        if p.exists():
            return str(p)
    except (OSError, ValueError):
        pass
    # ~/Models 下的目录（模型名 或 repo 名替换 -- 形式）
    for cand in (models_dir / model_id, models_dir / model_id.replace("/", "--")):
        try:
            if cand.is_dir() and (cand / "config.json").exists():
                return str(cand)
        except OSError:
            continue
    # LM Studio / Ollama 管理器目录（.lmstudio/models/<org>/<name> 或平铺）。
    # GGUF 的查找（_find_gguf）一直搜这里，MLX 目录此前不搜——用户在
    # LM Studio 下载的 MLX 模型明明在盘上，加载却回落 HF 联网下载。
    _name = model_id.split("/")[-1]
    for _mgr_root in (Path.home() / ".lmstudio" / "models", Path.home() / ".ollama" / "models"):
        try:
            if not _mgr_root.is_dir():
                continue
            for _org in _mgr_root.iterdir():
                if not _org.is_dir():
                    continue
                _cand = _org / _name
                if _cand.is_dir() and (_cand / "config.json").exists():
                    _fix_tokenizer_alias(_cand)
                    return str(_cand)
            _direct = _mgr_root / _name
            if _direct.is_dir() and (_direct / "config.json").exists():
                _fix_tokenizer_alias(_direct)
                return str(_direct)
        except OSError:
            continue
    # HF hub 缓存（models--owner--name/snapshots/<hash>/config.json）
    hub = hf_hub if hf_hub is not None else (Path.home() / ".cache" / "huggingface" / "hub")
    try:
        repo_dir = hub / f"models--{model_id.replace('/', '--')}"
        if repo_dir.is_dir():
            for snap in sorted(repo_dir.glob("snapshots/*")):
                if (snap / "config.json").exists():
                    return str(snap)
    except OSError:
        pass
    return model_id



def _custom_engine_target(model_id: str) -> tuple[str, str]:
    """自定义引擎的加载目标检查：返回 ("ok", 传给引擎的路径) 或 ("err", 说明)。

    - .gguf 文件 → 校验魔数；
    - 目录 → 内含 .gguf 就用那个文件（llama.cpp 系引擎要文件），
      否则要求有 config.json + model.safetensors（MLX/custom-runtime 包）。
    """
    p = Path(model_id)
    if p.is_file():
        try:
            with open(p, "rb") as f:
                return ("ok", str(p)) if f.read(4) == b"GGUF" else ("err", f"不是有效的 GGUF 文件: {p}")
        except Exception as e:
            return ("err", f"无法读取 {p}: {e}")
    if p.is_dir():
        ggufs = sorted(p.glob("*.gguf"))
        if ggufs:
            return ("ok", str(ggufs[0]))
        if (p / "config.json").is_file() and (p / "model.safetensors").is_file():
            return ("ok", str(p))
        return ("err", f"目录里既没有 .gguf，也没有 config.json + model.safetensors: {p}")
    return ("err", f"模型路径不存在: {model_id}")


def _mlx_reject_reason(model_id: str, model_type: str) -> tuple[str, str]:
    """MLX 引擎拒绝加载的**准确**原因（kind, 给用户看的话）。

    09-20 重写：旧实现只检查仓库里有没有 `preprocessor_config.json` 这类文件就断言
    "多模态（MLX-VLM）"——而很多**纯文本**模型也带这个文件（它只是处理器配置）。
    实测因此把 Prism 的 Bonsai MLX 包（**不含视觉塔**，但需要它自带的 runtime 与
    Hadamard codec）说成"需要 mlx-vlm 运行时"，把用户引到完全错误的方向。

    判据顺序（先因后果）：
      ① 自带运行时：`files.json` / `codec.py` / `artifact.py` / `runtime/*.py`；
      ② 真·多模态：`vision_utils.py` / `image_processing.py`，或 config.json 里有
         vision_config / vision_tower / image_token_index，或权重索引含 vision_tower./visual. 前缀；
      ③ 其它：架构未被 mlx-lm 收录。
    """
    d = Path(model_id)
    try:
        names = {f.name for f in d.iterdir()}
    except Exception:
        names = set()
    _rt_py = False
    try:
        _rt = d / "runtime"
        _rt_py = _rt.is_dir() and any(f.suffix == ".py" for f in _rt.iterdir())
    except Exception:
        _rt_py = False
    if _rt_py or ({"files.json", "codec.py", "artifact.py", "hadamard.json"} & names):
        return ("runtime",
                f"该模型需要它自带的运行时才能加载（架构 {model_type}）：仓库里带着自己的"
                "加载/解码实现（如 files.json / codec / hadamard 变换），而辣条的 MLX 引擎"
                "（mlx-lm）不执行仓库自带代码，也没有集成该运行时。"
                "建议：① 改用该模型的 GGUF 版本（这类模型转 GGUF 后通常保留文本能力）；"
                "② 换其它模型。")
    _vlm = bool(names & {"vision_utils.py", "image_processing.py"})
    cfg = {}
    try:
        cfg = json.loads((d / "config.json").read_text("utf-8"))
        if any(k in cfg for k in ("vision_config", "vision_tower", "image_token_index")):
            _vlm = True
    except Exception:
        pass
    if not _vlm:
        try:
            _idx = json.loads((d / "model.safetensors.index.json").read_text("utf-8"))
            _vlm = any(str(k).startswith(("vision_tower", "visual."))
                       for k in (_idx.get("weight_map") or {}))
        except Exception:
            pass
    if _vlm:
        return ("vlm",
                f"该模型是多模态（MLX-VLM）格式（架构 {model_type}），需要 mlx-vlm 运行时；"
                "Latiao 的 MLX 引擎（mlx-lm）未收录该架构，也未内置 mlx-vlm。"
                "建议：① 改用该模型的 GGUF 版本（转 GGUF 后通常只保留文本能力）；② 换其它模型。")
    return ("arch",
            f"MLX 引擎不支持该模型架构（{model_type}）。"
            "可用条件：该架构被 mlx-lm 内置支持，或 config.json 含 model_file 指向 MLX 实现。"
            "建议改用 GGUF 版本（若其架构被 llama.cpp 支持）或其它模型。")


def _mlx_arch_supported(model_dir: str) -> tuple[bool, str]:
    """MLX 启动前预检：mlx-lm 是否支持该架构（或仓库自带 MLX 实现）。

    背景（09-18 事故）：LM Studio 下载的 "…-MLX-8bit" 实为 Transformers 格式
    （config.model_type=zdtaichu5_0、无 model_file、仓库内 py 无任何 mlx 引用），
    mlx_lm.server 接单后直接挂死 → 就绪探测轮询到超时，界面永远"正在加载"。
    预检把这种必然失败提前成一句明确错误。
    """
    import json as _json
    try:
        cfg = _json.loads((Path(model_dir) / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return True, ""            # 读不到 config 不拦截（交给引擎报错）
    mt = str(cfg.get("model_type") or "")
    if not mt:
        return True, ""
    if cfg.get("model_file"):
        return True, ""            # 仓库自带 MLX 实现（mlx-lm 的 model_file 机制）
    try:
        import importlib as _imp
        _imp.import_module(f"mlx_lm.models.{mt}")
        return True, ""
    except Exception:
        return False, mt


# ggml 张量类型的 (每块元素数, 每块字节数)。用于从张量表算出"文件应有的最小长度"，
# 从而识别"下载未完成/被截断"。只列上游已知类型；遇到表外类型则该张量不计入长度
# （宁可算小也不误报——预检的完整性判断另有 1% 余量门限）。
_GGML_BLOCK = {
    0: (1, 1), 1: (1, 1), 2: (32, 18), 3: (32, 20), 6: (32, 22), 7: (32, 24),
    8: (32, 34), 9: (32, 36), 10: (256, 84), 11: (256, 110), 12: (256, 144),
    13: (256, 176), 14: (256, 210), 15: (256, 292), 16: (256, 66), 17: (256, 74),
    18: (256, 98), 19: (256, 50), 20: (32, 18), 21: (256, 110), 22: (256, 82),
    23: (256, 136), 24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8),
    29: (256, 56), 30: (1, 2), 34: (256, 54), 35: (256, 66), 39: (32, 17),
}
_GGML_TYPE_NAMES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0",
    9: "Q8_1", 10: "Q2_K", 11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K",
    15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S",
    20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16",
    26: "I32", 27: "I64", 28: "F64", 29: "IQ1_M", 30: "BF16", 34: "TQ1_0",
    35: "TQ2_0", 39: "MXFP4",
}
# 预检拦截门限：上游类型号目前 ~40，fork 私有量化常用大得多的编号（如 PQ2_0=142）。
# 只拦"明显不在任何上游枚举里"的编号，灰区（41–64）交给引擎判定 + 错误分类诊断。
_GGML_TYPE_SUSPECT = int(os.environ.get("LATIAO_GGML_TYPE_SUSPECT", "64") or 64)


def _config_file() -> Path:
    """config.json 路径（= PROGRESS_DIR/config.json）。

    local_llm 是底层模块，不能在顶层 import agent_loop（循环依赖），故惰性取。
    09-20 修正：此前本模块直接引用 CONFIG_FILE 却没导入 → NameError 被
    `except Exception` 吞掉 → **config.json 里的开关（external_engine / gguf_engine /
    custom_engine）实际从未生效**，只有环境变量那条路能用。
    """
    try:
        import agent_loop
        return agent_loop.CONFIG_FILE
    except Exception:
        return Path.home() / ".local-ai-os" / "config.json"


def _read_config() -> dict:
    """读 config.json（读不到/坏文件 → 空 dict，绝不抛）。"""
    try:
        return json.loads(_config_file().read_text("utf-8")) or {}
    except Exception:
        return {}


def _custom_engine_spec(model_path: str = "") -> dict:
    """自定义引擎（"路线 A"）：把任何 llama.cpp 系的可执行文件当辣条的本地引擎后端。

    场景：上游引擎读不了的模型（如 Prism/Bonsai 的三值包，私有 ggml 类型 142）——
    他们自带 fork 的 llama-server，辣条只负责起进程 + 按 OpenAI 协议对话，于是
    模型页加载/停止、状态、工具调用、缓存指标全部照常。

    config.json：
      "custom_engine": {
        "enabled": true,
        "binary": "/path/to/their/llama-server",
        "args": ["--jinja", "-ngl", "999"],   // 可选，追加在标准参数之后
        "name": "prism",                       // 可选，状态/日志显示用
        "match": "Bonsai"                      // 可选，仅当模型路径含该子串时启用
      }
    环境变量（覆盖配置）：LATIAO_CUSTOM_ENGINE_BIN / LATIAO_CUSTOM_ENGINE_ARGS；
    LATIAO_CUSTOM_ENGINE=0 临时关闭。返回 {} = 未启用（走辣条自带引擎）。
    """
    _cfg_all = _read_config()
    cfg = _cfg_all.get("custom_engine") if isinstance(_cfg_all, dict) else None
    # 支持单个对象或**列表**（09-20）：同一模型家族常有多个包（GGUF 用 fork 引擎、
    # MLX 用自带运行时），各自需要不同的 binary → 列表里按 match 选第一条命中的。
    if isinstance(cfg, list):
        _cand = [c for c in cfg if isinstance(c, dict)]
    elif isinstance(cfg, dict):
        _cand = [cfg]
    else:
        _cand = []
    if not _cand:
        return {}
    cfg = next((c for c in _cand
                if not c.get("match")
                or str(c["match"]).lower() in str(model_path).lower()), _cand[0])
    if os.environ.get("LATIAO_CUSTOM_ENGINE", "").strip().lower() in ("0", "false", "off", "no"):
        return {}
    if cfg.get("enabled") is False:
        return {}   # 显式 enabled:false → 保持关闭（配好路径先放着、要用时再开）
    _bin = os.environ.get("LATIAO_CUSTOM_ENGINE_BIN", "") or str(cfg.get("binary") or "")
    if not _bin:
        return {}
    binary = Path(_bin).expanduser()
    if not binary.exists():
        logger.warning("自定义引擎二进制不存在，忽略 custom_engine: %s", binary)
        return {}
    _match = str(cfg.get("match") or "")
    if _match and _match.lower() not in str(model_path).lower():
        return {}
    _args_env = os.environ.get("LATIAO_CUSTOM_ENGINE_ARGS", "")
    if _args_env:
        import shlex
        args = shlex.split(_args_env)
    else:
        args = [str(a) for a in (cfg.get("args") or []) if str(a).strip()]
    return {"binary": str(binary), "args": args,
            "name": str(cfg.get("name") or "custom")}


def _gguf_scan(model_path: str) -> dict | None:
    """读 GGUF：架构、张量类型直方图、张量数据所需的最小文件长度。

    本地模型最高频的两类失败都能在这里识别：
      ① fork 私有量化类型（引擎报 "has invalid ggml type N"，如 Ternary-Bonsai 的 PQ2_0=142）；
      ② 下载未完成/被截断（引擎报 "data is not within the file bounds"）。
    解析不动就返回 None —— **fail-open**：绝不因为读不懂文件而拦住加载。
    """
    import struct as _st
    try:
        with open(model_path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            _st.unpack("<I", f.read(4))                     # version
            n_tensors, n_kv = _st.unpack("<QQ", f.read(16))

            def _rs() -> bytes:
                n = _st.unpack("<Q", f.read(8))[0]
                if n > 1 << 24:                              # 异常长度 → 放弃
                    raise ValueError("bad string length")
                return f.read(n)

            _fixed = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
                      10: 8, 11: 8, 12: 8}
            arch = ""
            for _ in range(n_kv):
                key = _rs().decode("utf-8", "replace")
                t = _st.unpack("<I", f.read(4))[0]
                if t == 8:
                    val = _rs().decode("utf-8", "replace")
                elif t == 9:                                 # 数组
                    et = _st.unpack("<I", f.read(4))[0]
                    cnt = _st.unpack("<Q", f.read(8))[0]
                    if et == 8:
                        for _ in range(cnt):
                            _rs()
                    elif et in _fixed:
                        f.seek(_fixed[et] * cnt, 1)
                    else:
                        return None
                    val = ""
                elif t in _fixed:
                    # 只看架构（字符串）；其余值按长度跳过——不要再按 4 字节解包
                    # （bool=1 字节会把缓冲区解崩，09-20 修正）
                    _raw = f.read(_fixed[t])
                    val = str(_st.unpack("<I", _raw)[0]) if t == 4 else ""
                else:
                    return None
                if key == "general.architecture":
                    arch = str(val)

            _data_start = f.tell()
            types: dict[int, int] = {}
            max_end = 0
            for _ in range(n_tensors):
                _rs()                                        # 名称
                nd = _st.unpack("<I", f.read(4))[0]
                numel = 1
                for _ in range(nd):
                    numel *= _st.unpack("<Q", f.read(8))[0]
                tt = _st.unpack("<I", f.read(4))[0]
                off = _st.unpack("<Q", f.read(8))[0]
                types[tt] = types.get(tt, 0) + 1
                blk = _GGML_BLOCK.get(tt)
                if blk and numel % blk[0] == 0:
                    max_end = max(max_end, off + (numel // blk[0]) * blk[1])
        import os as _os
        return {
            "arch": arch,
            "types": types,
            "data_start": _data_start,
            "need_bytes": _data_start + max_end if max_end else 0,
            "size": _os.path.getsize(model_path),
        }
    except Exception:
        logger.debug("GGUF 扫描失败（fail-open）: %s", model_path, exc_info=True)
        return None


def _gguf_precheck(model_path: str) -> str:
    """加载前的 GGUF 预检：返回错误提示（空串=放行）。

    把两类**注定失败**的情况提前拦住，避免"启动引擎 → 失败 → 猜原因"：
      ① 量化类型号明显不在上游枚举内（fork 私有的低位量化，如 Ternary-Bonsai 的
         PQ2_0 = 类型 142，共 402/851 个张量）——引擎只会说 "invalid ggml type 142"；
      ② 文件长度明显短于张量表要求（下载未完成/被截断）——引擎只会说
         "data is not within the file bounds"。
    两者在 09-20 都被错误地归因成"架构不支持"，把用户引向"换 MLX / 等上游支持架构"。
    读不动文件时一律放行（fail-open，绝不因为我们解析失败而拦住加载）。
    """
    r = _gguf_scan(model_path)
    if not r:
        return ""
    bad = {t: c for t, c in r["types"].items() if t > _GGML_TYPE_SUSPECT}
    if bad:
        _d = "、".join(f"{_ggml_type_label(t)}（{c} 个张量）" for t, c in sorted(bad.items()))
        return ("该模型的量化格式不被当前引擎支持：" + _d + "。"
                "这通常不是架构问题，而是某个 fork 自造的私有量化（上游 llama.cpp 未收录该类型号）。"
                "可选：① 换用产出该量化的那个 fork 的引擎；"
                "② 换用同一模型的常规量化版本（Q4_K_M / Q8_0 等）；"
                "③ 若确实要低位/三值量化，请选上游标准写法（如 TQ2_0）。")
    need, size = int(r.get("need_bytes") or 0), int(r.get("size") or 0)
    if need and size < need * 0.995:
        return (f"模型文件不完整（下载未完成或文件损坏）：按张量表至少需要 {need/1e9:.2f} GB，"
                f"当前 {size/1e9:.2f} GB，还缺 {(need-size)/1e9:.2f} GB。"
                "请等下载完成（或重新下载）后再加载。")
    return ""


def _ggml_type_label(t: int) -> str:
    return f"{_GGML_TYPE_NAMES[t]}({t})" if t in _GGML_TYPE_NAMES else f"类型 {t}"


def _gguf_architecture(model_path: str) -> str:
    """从 GGUF 头读 general.architecture（用于把"未知架构"报错翻成人话）。"""
    import struct as _st
    try:
        with open(model_path, "rb") as f:
            if f.read(4) != b"GGUF":
                return ""
            f.read(4)                                  # version
            _st.unpack("<Q", f.read(8))                # tensor count
            n_kv = _st.unpack("<Q", f.read(8))[0]
            def _str():
                n = _st.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", "replace")
            for _ in range(min(n_kv, 16)):
                k = _str()
                t = _st.unpack("<I", f.read(4))[0]
                if t == 8:
                    v = _str()
                elif t == 4:
                    v = str(_st.unpack("<I", f.read(4))[0])
                elif t == 10:
                    v = str(_st.unpack("<Q", f.read(8))[0])
                elif t == 6:
                    v = str(_st.unpack("<f", f.read(4))[0])
                elif t == 7:
                    v = str(_st.unpack("<?", f.read(1))[0])
                else:
                    break
                if k == "general.architecture":
                    return v
    except Exception:
        return ""
    return ""


def _auto_cache_type(model_path: str) -> tuple[int, int]:
    """Return (type_k, type_v) as ggml_type ints based on model quantization level.
    KV cache precision should never exceed model precision.
    ggml_type: F16=1, Q4_0=2, Q8_0=8"""
    bits = _detect_model_bits(model_path)
    if bits <= 4:
        return (2, 2)    # Q4 model → Q4_0 KV (max memory savings)
    elif bits <= 8:
        return (8, 8)    # Q5-Q8 model → Q8_0 KV (balanced)
    else:
        return (8, 8)    # F16+ model → Q8_0 KV

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"
IS_APPLE_SILICON = IS_MAC and (platform.processor() == "arm" or "Apple" in platform.processor())


