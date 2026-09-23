"""身份改名的识别与"单一事实源"（2026-09-23 真机事故）。

事故经过（用户在设置页发现）：
- 用户让助手改叫"欧娜"，行为里也确实叫欧娜了（截图），但设置页"辣条的名字"仍是辣条。
- 日志证据：`Tool result: write_file → ✅ 已写入：/Users/langzuxiang/.local-ai-os/USER.md`
  —— **模型自己**把名字写进了 USER.md；而 IDENTITY.md 上次修改还是 9 月 18 日。
- 两个原因叠加：
  ① 改名识别太窄（"你以后就叫欧娜吧"这类自然说法没命中）→ IDENTITY.md 从未被更新；
  ② 提示里没写"名字只写 IDENTITY.md"→ 模型自造了第二个事实源，于是
     **行为读 USER.md（欧娜）、设置页读 IDENTITY.md（辣条）** 对不上。

这里守住：识别矩阵、写入→卡片读取的往返契约（就是事故的形状）、冲突告警。
"""
import importlib
import logging

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LATIAO_TEST_PROGRESS_DIR", str(tmp_path / ".local-ai-os"))
    import config
    importlib.reload(config)
    import identity
    importlib.reload(identity)
    import onboarding
    importlib.reload(onboarding)
    return identity, onboarding


# ── 识别矩阵（真机漏掉的那几种必须命中，问句必须不命中）──────────────

@pytest.mark.parametrize("text,expect_file,expect_value", [
    ("以后叫你欧娜", "IDENTITY.md", "欧娜"),
    ("你以后就叫欧娜吧", "IDENTITY.md", "欧娜"),          # ← 事故里最可能的那句
    ("你的名字是欧娜", "IDENTITY.md", "欧娜"),
    ("名字改成欧娜", "IDENTITY.md", "欧娜"),
    ("把名字换为欧娜", "IDENTITY.md", "欧娜"),            # 长优先：别把"为欧娜"留进名字
    ("名字叫欧娜", "IDENTITY.md", "欧娜"),
    ("改名为欧娜", "IDENTITY.md", "欧娜"),
    ("我叫你欧娜", "IDENTITY.md", "欧娜"),
    ("我的名字是张三", "USER.md", "张三"),
    ("我叫李四", "USER.md", "李四"),
])
def test_rename_phrasings_are_detected(env, text, expect_file, expect_value):
    identity, _ = env
    intents = identity._detect_identity_intent(text)
    assert intents, f"没识别出来：{text}"
    assert intents[0]["file"] == expect_file
    assert intents[0]["value"] == expect_value
    assert intents[0]["action"] in ("name", "user_name")


@pytest.mark.parametrize("text", [
    "你叫什么名字", "你的名字是什么？", "你叫啥", "你叫谁", "帮我改个名字",
])
def test_questions_are_not_renames(env, text):
    """问句绝不能被当改名——否则一句"你叫啥"就会把"啥"写成名字（矩阵实测过一次）。"""
    identity, _ = env
    assert identity._detect_identity_intent(text) == []


# ── 往返契约：写入之后，设置卡片必须读到新名字（事故的形状）──────────

def test_rename_round_trips_to_settings_card(env):
    identity, onboarding = env
    root = identity.PROGRESS_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "IDENTITY.md").write_text(
        "# Identity\n\n你的名字是「辣条」，英文名 Latiao。\n", encoding="utf-8")

    assert onboarding._read_agent_name() == "辣条"
    result = identity._process_identity_intents("你以后就叫欧娜吧")
    assert result and "IDENTITY.md" in result, f"意图未被应用：{result}"
    # 这就是事故的判据：卡片读到的名字必须变成新名字
    assert onboarding._read_agent_name() == "欧娜"
    assert "你的名字是「欧娜」" in (root / "IDENTITY.md").read_text(encoding="utf-8")


# ── 冲突告警：名字在两个文件里不一致时必须留痕 ──────────────────────

def test_name_conflict_warns(env, caplog):
    identity, _ = env
    root = identity.PROGRESS_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "IDENTITY.md").write_text("# Identity\n\n你的名字是「辣条」。\n", encoding="utf-8")
    (root / "USER.md").write_text("# User Profile\n\n- **名字**：欧娜（原\"辣条/Latiao\"）\n",
                                  encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        msgs = identity._read_identity()
    assert [m["file"] for m in msgs] == ["IDENTITY.md", "USER.md"]
    joined = "\n".join(r.message for r in caplog.records)
    assert "名字冲突" in joined and "辣条" in joined and "欧娜" in joined


def test_no_warning_when_names_agree(env, caplog):
    identity, _ = env
    root = identity.PROGRESS_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "IDENTITY.md").write_text("# Identity\n\n你的名字是「欧娜」。\n", encoding="utf-8")
    (root / "USER.md").write_text("# User Profile\n\n- **名字**：欧娜\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        identity._read_identity()
    assert "名字冲突" not in "\n".join(r.message for r in caplog.records)
