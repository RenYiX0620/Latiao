"""插件 seed 的两条守卫（审计 A5/A6）：

A5 —— 内嵌在 tool_system._SEED_PLUGINS 的副本必须与 sidecar/plugins/ 的现行文件
      逐字节一致。不守住的话，"让 seed 生效"的第一步就是用旧副本把现行插件覆写
      回退（实测：run_cmd 长任务超时 300s→30s、read_file 丢掉 PROGRESS 摘要）。
A6 —— 有文件但无基线记录时必须**记基线而不是永远跳过**。旧实现让 manifest 恒为
      {} → 任何 seed 更新都下发不到已装机器。
"""
import json
from pathlib import Path

import pytest

import tool_system as ts


def test_embedded_seeds_match_disk_files():
    """A5：每个内嵌 seed 与磁盘现行插件逐字节一致（漂移即失败）。"""
    drifted = []
    for name, text in ts._SEED_PLUGINS.items():
        f = Path(ts.__file__).parent / "plugins" / name
        assert f.exists(), f"内嵌了 {name} 但磁盘上没有该插件"
        if f.read_text("utf-8") != text:
            drifted.append(name)
    assert not drifted, (
        f"内嵌 seed 与磁盘插件漂移：{drifted}。"
        "修法：用脚本从 sidecar/plugins/ 重新生成 _SEED_PLUGINS（保持逐字节）")


@pytest.fixture()
def plugins_tmp(monkeypatch, tmp_path):
    """把 PLUGINS_DIR 指到临时目录，单独驱动 _seed_default_plugins。"""
    monkeypatch.setattr(ts, "PLUGINS_DIR", tmp_path, raising=False)
    return tmp_path


def test_baseline_recorded_for_unmodified_existing_files(plugins_tmp):
    """A6：文件已存在且与现行 seed 一致（=随包发的原样）→ 记基线、不动文件。

    基线记上以后，后续版本的 seed 更新才可能下发（旧实现 manifest 恒为 {}）。"""
    name = next(iter(ts._SEED_PLUGINS))
    disk_text = (Path(ts.__file__).parent / "plugins" / name).read_text("utf-8")
    (plugins_tmp / name).write_text(disk_text, encoding="utf-8")

    ts._seed_default_plugins()

    manifest = json.loads((plugins_tmp / ".seed_manifest.json").read_text("utf-8"))
    assert manifest.get(name) == ts._sha256(disk_text), "未记录基线 → seed 永远不下发"
    assert (plugins_tmp / name).read_text("utf-8") == disk_text, "不该覆盖现有文件"


def test_seed_update_applies_when_unmodified(plugins_tmp):
    """A6：基线之后，未改动过的文件会被新 seed 刷新（这才是"更新能下发"）。"""
    name = next(iter(ts._SEED_PLUGINS))
    baked = dict(ts._SEED_PLUGINS)
    # 第一次：文件与现行 seed 一致 → 建立基线（模拟随包发的插件）
    (plugins_tmp / name).write_text(ts._SEED_PLUGINS[name], encoding="utf-8")
    ts._seed_default_plugins()
    manifest = json.loads((plugins_tmp / ".seed_manifest.json").read_text("utf-8"))
    assert manifest[name] == ts._sha256(ts._SEED_PLUGINS[name])

    # 第二次：seed 更新了（模拟版本升级），基线匹配 → 应被刷新
    ts._SEED_PLUGINS = {name: "NEW CONTENT\n"}
    try:
        ts._seed_default_plugins()
    finally:
        ts._SEED_PLUGINS = baked
    assert (plugins_tmp / name).read_text("utf-8") == "NEW CONTENT\n"


def test_user_modified_file_not_overwritten(plugins_tmp):
    """用户手改过的文件必须原样保留：内容既不等于现行 seed 又无基线时，
    不建基线也不刷新（保守策略——宁可少刷新，不丢用户编辑）。"""
    name = next(iter(ts._SEED_PLUGINS))
    baked = dict(ts._SEED_PLUGINS)
    (plugins_tmp / name).write_text("USER EDIT\n", encoding="utf-8")
    ts._seed_default_plugins()                       # 无基线 + 不等于 seed → 什么都不做
    manifest = json.loads((plugins_tmp / ".seed_manifest.json").read_text("utf-8"))
    assert name not in manifest, "不该给无法判断的文件建基线"

    ts._SEED_PLUGINS = {name: "NEW SEED\n"}
    try:
        ts._seed_default_plugins()
    finally:
        ts._SEED_PLUGINS = baked
    assert (plugins_tmp / name).read_text("utf-8") == "USER EDIT\n", "用户改动被覆盖了"


def test_missing_file_is_restored(plugins_tmp):
    """文件丢失 → 从内嵌 seed 装回来（这条是 seed 存在的本来目的）。"""
    name = next(iter(ts._SEED_PLUGINS))
    ts._seed_default_plugins()
    assert (plugins_tmp / name).read_text("utf-8") == ts._SEED_PLUGINS[name]


def test_one_unwritable_file_does_not_skip_the_rest(plugins_tmp, monkeypatch):
    """A6 补充：某个文件写失败（只读安装位置）不能连累后面的文件。"""
    names = list(ts._SEED_PLUGINS)[:3]
    real_write = ts._atomic_write
    calls = []

    def _flaky(path, text):
        calls.append(Path(path).name)
        if Path(path).name == names[0]:
            raise OSError("read-only file system")
        return real_write(path, text)

    monkeypatch.setattr(ts, "_atomic_write", _flaky, raising=False)
    ts._seed_default_plugins()
    assert names[1] in calls and names[2] in calls, f"后续文件被跳过：{calls}"
    assert (plugins_tmp / names[1]).exists() and (plugins_tmp / names[2]).exists()
