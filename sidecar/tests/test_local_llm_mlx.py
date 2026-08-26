"""MLX 模型路径解析与目录扫描测试（临时目录构造，不依赖真实模型）。"""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent.parent))

from local_llm import _resolve_mlx_path, list_local_models  # noqa: E402


class TestMlxResolvePath(unittest.TestCase):
    def _make_models_dir(self) -> TemporaryDirectory:
        """构造 MLX 目录（config.json + model.safetensors）与假权重库。"""
        td = TemporaryDirectory()
        root = Path(td.name)

        # MLX 目录（目录名下）
        mlx_dir = root / "Qwen3-0.6B-4bit"
        mlx_dir.mkdir()
        (mlx_dir / "config.json").write_text("{}")
        (mlx_dir / "model.safetensors").write_bytes(b"\x00" * 16)

        # repo 名 -- 形式目录
        repo_dir = root / "mlx-community--Qwen3-0.6B-4bit"
        repo_dir.mkdir()
        (repo_dir / "config.json").write_text("{}")
        (repo_dir / "weights.npz").write_bytes(b"\x00" * 16)

        # 非 MLX 目录（缺权重）——不应被识别
        (root / "not-a-model").mkdir()
        (root / "not-a-model" / "config.json").write_text("{}")

        return td

    def test_resolve_models_dir(self):
        with self._make_models_dir() as td:  # noqa: SIM117
            pass
        td = self._make_models_dir()
        root = Path(td.name)
        try:
            # 目录名 → 命中
            self.assertEqual(
                _resolve_mlx_path("Qwen3-0.6B-4bit", models_dir=root),
                str(root / "Qwen3-0.6B-4bit"),
            )
            # repo 名 -- 形式 → 命中
            self.assertEqual(
                _resolve_mlx_path("mlx-community/Qwen3-0.6B-4bit", models_dir=root),
                str(root / "mlx-community--Qwen3-0.6B-4bit"),
            )
            # 不存在的 → 原样返回（当作 HF repo id）
            self.assertEqual(_resolve_mlx_path("mlx-community/xxx", models_dir=root), "mlx-community/xxx")
            # 直接路径 → 直接返回
            p = str(root / "Qwen3-0.6B-4bit")
            self.assertEqual(_resolve_mlx_path(p, models_dir=root), p)
            # 纯目录名但未存在 → 原样返回（mlx_lm 自行处理）
            self.assertEqual(_resolve_mlx_path("nope", models_dir=root), "nope")
        finally:
            td.cleanup()

    def test_resolve_hf_hub(self):
        with TemporaryDirectory() as td:
            hub = Path(td) / "hub"
            repo = hub / "models--mlx-community--Qwen3-0.6B-4bit" / "snapshots" / "abc123"
            repo.mkdir(parents=True)
            (repo / "config.json").write_text("{}")
            self.assertEqual(
                _resolve_mlx_path("mlx-community/Qwen3-0.6B-4bit", hf_hub=hub),
                str(repo),
            )


if __name__ == "__main__":
    unittest.main()
