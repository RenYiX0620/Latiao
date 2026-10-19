"""控制类工具的 runlogs 必须跟随 PROGRESS_DIR（2026-10-19 跟进）。

外部核验在 workspace-write 沙箱里跑套件时有 4 个用例失败，报错是
`❌ 启动失败: [Errno 1] Operation not permitted: ~/.local-ai-os/runlogs/proc_*.out`。
根因：`_control_common.RUNLOGS` 写死在 `Path.home()`，conftest 的
LATIAO_TEST_PROGRESS_DIR 重定向管不到它——测试会写用户真机目录（沙箱里则直接失败）。
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _load_control_common():
    f = ROOT / "plugins" / "_control_common.py"
    spec = importlib.util.spec_from_file_location("cc_runlogs_test", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runlogs_follows_progress_dir():
    mod = _load_control_common()
    assert str(mod.RUNLOGS).startswith(str(config.PROGRESS_DIR)), \
        f"runlogs 没跟随 PROGRESS_DIR: {mod.RUNLOGS}"


def test_runlogs_is_not_hardcoded_to_home():
    src = (ROOT / "plugins" / "_control_common.py").read_text("utf-8")
    assert 'Path.home() / ".local-ai-os" / "runlogs"' not in src, \
        "runlogs 又写死回用户真机路径了"
