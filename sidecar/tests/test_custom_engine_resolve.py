"""custom_engine 分支的 model_id 归一（_resolve_custom_model_path）验收测试。

2026-10-03 occamy 事故：自动重载传来的 model_id 是裸名（无路径），custom 分支
此前直接查路径，必死于"模型路径不存在"且不留 ERROR 日志——重载线程静默失败，
用户只看到"端口无监听"。修法 = 复用标准分支的 _find_gguf 解析（LM Studio 目录
布局返回内部同名主文件，天然跳过 mmproj）。

全部用封闭临时目录，不依赖本机模型。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_llm_engine


def _bare_engine() -> local_llm_engine.EngineProcess:
    e = local_llm_engine.EngineProcess.__new__(local_llm_engine.EngineProcess)
    e._gguf_find_cache = {}
    return e


class TestResolveCustomModelPath(unittest.TestCase):
    def test_existing_file_passthrough(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m.gguf"
            f.write_bytes(b"GGUF\0\0\0\0")
            self.assertEqual(
                _bare_engine()._resolve_custom_model_path(str(f)), str(f))

    def test_existing_dir_passthrough(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                _bare_engine()._resolve_custom_model_path(d), d)

    def test_unknown_name_returns_original(self):
        # 机器上不存在的怪名 → _find_gguf 扫不到 → 返回原值，
        # 让 _custom_engine_target 给出可读错误（而不是在这里抛）。
        name = "zz-definitely-not-a-model-9f3a"
        self.assertEqual(
            _bare_engine()._resolve_custom_model_path(name), name)

    def test_bare_name_resolves_to_inner_main_file_not_mmproj(self):
        # LM Studio 布局：裸名 → 模糊命中包内主文件，mmproj 不截胡
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "zz-model-x7.gguf"
            pkg.mkdir()
            main = pkg / "zz-model-x7.gguf"
            main.write_bytes(b"GGUF\0\0\0\0")
            (pkg / "mmproj-zz.gguf").write_bytes(b"GGUF\0\0\0\0")
            with mock.patch.object(local_llm_engine, "MODELS_DIR", Path(d)):
                got = _bare_engine()._resolve_custom_model_path("zz-model-x7")
            self.assertEqual(got, str(main))

    def test_gguf_suffixed_dir_resolves_to_inner_main_file(self):
        # 目录直达时 _resolve_custom_model_path 放行，但 _custom_engine_target
        # 的目录分支必须优先取内部同名主文件——sorted[0] 会按字母序选中
        # mmproj（m < o）当主模型。
        from local_llm_probe import _custom_engine_target
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "zz-model-y9.gguf"
            pkg.mkdir()
            main = pkg / "zz-model-y9.gguf"
            main.write_bytes(b"GGUF\0\0\0\0")
            (pkg / "mmproj-zz.gguf").write_bytes(b"GGUF\0\0\0\0")
            ok, target = _custom_engine_target(str(pkg))
            self.assertEqual(ok, "ok")
            self.assertEqual(target, str(main))


if __name__ == "__main__":
    unittest.main()
