"""测试共享配置（conftest）。

关键：场景测试会真实执行 agent 循环并写入进度/DB/memory 等文件——
pytest_configure 在收集/导入任何测试模块之前把 PROGRESS_DIR 重定向到
临时目录（config.py 支持 LATIAO_TEST_PROGRESS_DIR 覆盖），避免污染真机
~/.local-ai-os（此前 PROGRESS.md 被场景测试越写越大，见阶段 2b 基线提交）。

**真配置底线守卫（2026-10-11 事故后加）**：某个 fixture 若在 teardown 里
importlib.reload(config) 而不恢复环境变量，会把 config.CONFIG_FILE 留在**真机
路径**上——之后任何模块走"兜底持久化"（api_routes 按请求把 cloud_config 写
config.json）都会覆盖用户真配置。当天实测：test_anon_usage 的 fixture 泄漏 →
test_app_flow 的假 cloud_config（http://unused）把用户 1100 字节的 config.json
覆盖成 165 字节。守卫两道：① 每条用例结束时核对真 config 是否被写（写坏就
还原并让该用例红脸）；② 检测到 CONFIG_FILE 指向真机路径时告警。
"""
import os
import shutil
import tempfile
from pathlib import Path

_REAL_CONFIG = Path.home() / ".local-ai-os" / "config.json"
_SESSION_BACKUP = Path(tempfile.gettempdir()) / "latiao-pytest-confighash-backup.json"


def pytest_configure(config):
    if not os.environ.get("LATIAO_TEST_PROGRESS_DIR"):
        os.environ["LATIAO_TEST_PROGRESS_DIR"] = tempfile.mkdtemp(
            prefix="latiao-test-progress-")
    # 会话级备份：真 config 在测试开始前的样子（守卫拿它做还原源）
    try:
        if _REAL_CONFIG.exists():
            shutil.copy2(_REAL_CONFIG, _SESSION_BACKUP)
    except OSError:
        pass
    # 真机更新状态同样备份（守卫源；见文件下方 _guard_real_update_state）
    try:
        _REAL_UPDATE_STATE.parent.mkdir(parents=True, exist_ok=True)
        if _REAL_UPDATE_STATE.exists():
            shutil.copy2(_REAL_UPDATE_STATE, _UPDATE_STATE_BACKUP)
    except OSError:
        pass


def _real_config_digest() -> str:
    import hashlib
    try:
        return hashlib.md5(_REAL_CONFIG.read_bytes()).hexdigest()
    except OSError:
        return "(missing)"


def pytest_sessionstart(session):
    session.config._latiao_real_cfg_digest = _real_config_digest()


def pytest_sessionfinish(session, exitstatus):
    """会话结束清掉本会话的测试临时目录（2026-10-12 磁盘事故修复）。

    为什么必须清：conftest 每次会话建一个 `latiao-test-progress-*` 目录，
    其中 `update/` 会带上整包级别的下载残片（每个 ~310MB）。实测积到
    **322 个目录、约 5.7GB** 一直留在系统临时目录里（Finder 里计入「系统
    数据」）。此前从不回收——每次 pytest 都加一份。现在会话结束即删。
    """
    d = os.environ.get("LATIAO_TEST_PROGRESS_DIR", "")
    if d and "latiao-test-progress-" in d and os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


import pytest  # noqa: E402

# 真机更新状态（与 config 同级的"测试可污染的生产文件"）：2026-10-11 实测被
# 一个没走隔离夹具的测试类写进假状态（total=14、part 指向测试临时目录）——
# 用户随后看到"正在下载新版本 … 天文数字%（断线自动续传）"且永远不会完成。
_REAL_UPDATE_STATE = Path.home() / ".local-ai-os" / "update" / "state.json"
_UPDATE_STATE_BACKUP = Path(tempfile.gettempdir()) / "latiao-pytest-update-state-backup.json"


def _update_state_digest() -> str:
    import hashlib
    try:
        return hashlib.md5(_REAL_UPDATE_STATE.read_bytes()).hexdigest()
    except OSError:
        return "(missing)"


@pytest.fixture(autouse=True)
def _guard_real_update_state():
    """每次用例前后核对真机 update/state.json：被测试改写 → 还原 + 该用例失败。

    为什么需要：update_service 的 STATE_FILE 在**导入时**绑定到 PROGRESS_DIR，
    而夹具的重定向只改模块属性、救不了"直接调全局落盘路径"的测试；漏一个就会
    把假状态写进用户更新目录（已真实发生过一次）。
    """
    before = _update_state_digest()
    existed_before = _UPDATE_STATE_BACKUP.exists()
    yield
    after = _update_state_digest()
    if after != before:
        try:
            if existed_before:
                shutil.copy2(_UPDATE_STATE_BACKUP, _REAL_UPDATE_STATE)
            elif _REAL_UPDATE_STATE.exists():
                _REAL_UPDATE_STATE.unlink()
        except OSError:
            pass
        raise AssertionError(
            f"测试改写了真机 update/state.json（{before} → {after}）！已还原；"
            "请让该测试走 tests/test_update_service.py 的隔离夹具（重定向 "
            "update_service.STATE_FILE/UPDATE_DIR 并 mock 落盘）。"
        )


@pytest.fixture(autouse=True)
def _guard_real_config():
    """每条用例前后核对真 config：被测试写坏 → 还原 + 该用例失败。

    两道检查：
    ① **开跑前**：config.CONFIG_FILE 若指向真机路径 → 直接让该用例失败
       （2026-10-11 泄漏事故的预防档：某夹具把 CONFIG_FILE 固化成真机路径后，
       后续任何走"兜底持久化"的用例都会写真文件——在这里当场拦停在**写之前**，
       而不是等写坏再还原）。
    ② **跑完后**：真 config md5 变了 → 从会话备份还原 + 失败。
    干净的用例零开销（一次属性读取 + 两次 md5）。
    """
    import config as _config_mod
    cfg_path = str(getattr(_config_mod, "CONFIG_FILE", ""))
    if cfg_path and cfg_path == str(_REAL_CONFIG):
        raise AssertionError(
            f"config.CONFIG_FILE 指向真机路径（{cfg_path}）——测试读写的必须是临时目录！"
            "多半是某个夹具 teardown 里 delenv + reload 留下的；"
            "正确写法是先恢复 LATIAO_TEST_PROGRESS_DIR 再 reload。"
        )
    before = _real_config_digest()
    existed_before = _SESSION_BACKUP.exists()
    yield
    after = _real_config_digest()
    if after != before:
        # 还原到会话开始时的样子（宁可回滚，也不让真配置留在测试态）
        try:
            if existed_before:
                shutil.copy2(_SESSION_BACKUP, _REAL_CONFIG)
                os.chmod(_REAL_CONFIG, 0o600)
            elif _REAL_CONFIG.exists():
                # 会话开始时本就没有真配置（CI/新机）→ 测试造出来的要清掉
                _REAL_CONFIG.unlink()
        except OSError:
            pass
        raise AssertionError(
            f"测试改写了真机 config.json（{before} → {after}）！"
            "已还原；请检查是哪个 fixture 没把 LATIAO_TEST_PROGRESS_DIR 隔离好。"
        )
