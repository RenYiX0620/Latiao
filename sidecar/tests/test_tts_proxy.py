"""语音合成代理（tts_service）单测：配置合并、降级语义、成功/失败两条路径。

第一轮的设计约定（改了要同步这里）：
  · 服务不在 → `code=tts_unavailable` + `next_steps`（前端据此回退系统语音，不显示超时）
  · 关掉开关 → `code=tts_disabled`
  · 服务报错 → `code=tts_service_error`（带服务返回的 detail）
  · 成功 → 返回音频字节 + content-type，**不经过 base64**（省 1/3 体积）
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tts_service  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    tts_service._probe_cache.update({"at": 0.0, "base": "", "ok": False})
    yield
    tts_service._probe_cache.update({"at": 0.0, "base": "", "ok": False})


def _cfg(tmp_path, tts=None) -> Path:
    p = tmp_path / "config.json"
    data = {"tavily_api_key": "x"}
    if tts is not None:
        data["tts"] = tts
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ── 配置 ────────────────────────────────────────────────────────
def test_config_defaults_when_missing(tmp_path):
    cfg = tts_service.read_tts_config(_cfg(tmp_path))
    assert cfg["enabled"] is True
    assert cfg["base_url"] == tts_service.DEFAULT_TTS["base_url"]
    assert cfg["speed"] == 1.0


def test_config_merges_and_clamps_speed(tmp_path):
    cfg = tts_service.read_tts_config(_cfg(tmp_path, {"base_url": "http://127.0.0.1:9999/",
                                                     "speed": 9, "model_id": "melotts"}))
    assert cfg["base_url"] == "http://127.0.0.1:9999"   # 尾部斜杠去掉
    assert cfg["speed"] == 2.0                           # 夹到上限
    assert cfg["model_id"] == "melotts"
    assert cfg["enabled"] is True                        # 未提供的键回落默认


def test_config_survives_broken_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not json", encoding="utf-8")
    assert tts_service.read_tts_config(p)["enabled"] is True


# ── 降级语义（前端就靠 code 决定是否回退）────────────────────────
def test_unavailable_payload_shape():
    payload = tts_service.unavailable_payload({"base_url": "http://127.0.0.1:7799"})
    assert payload["status"] == "error"
    assert payload["code"] == "tts_unavailable"
    assert payload["next_steps"] and isinstance(payload["next_steps"], list)


@pytest.mark.asyncio
async def test_synthesize_service_down_returns_structured_error(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_service, "probe_service", lambda conf: False)
    audio, ctype, err = await tts_service.synthesize("你好", None, None, _cfg(tmp_path))
    assert audio is None and ctype is None
    assert err["code"] == "tts_unavailable"
    assert "系统语音" in " ".join(err["next_steps"])


@pytest.mark.asyncio
async def test_synthesize_disabled(tmp_path):
    audio, _, err = await tts_service.synthesize("你好", None, None,
                                                 _cfg(tmp_path, {"enabled": False}))
    assert audio is None and err["code"] == "tts_disabled"


@pytest.mark.asyncio
async def test_synthesize_empty_text(tmp_path):
    _, _, err = await tts_service.synthesize("   ", None, None, _cfg(tmp_path))
    assert err["code"] == "tts_empty_text"


# ── 成功路径（stub 掉 httpx，不联网）────────────────────────────
class _FakeResp:
    def __init__(self, status=200, content=b"RIFFwav", ctype="audio/wav", body=None):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype}
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.posts.append((url, json))
        return self._resp

    async def get(self, url):
        self.posts.append((url, None))
        return self._resp


def _stub(monkeypatch, resp):
    client = _FakeClient(resp)
    monkeypatch.setattr(tts_service, "probe_service", lambda conf: True)
    monkeypatch.setattr(tts_service.httpx, "AsyncClient", lambda **kw: client)
    return client


@pytest.mark.asyncio
async def test_synthesize_success_returns_bytes(tmp_path, monkeypatch):
    client = _stub(monkeypatch, _FakeResp(content=b"RIFF-audio-bytes"))
    audio, ctype, err = await tts_service.synthesize(
        "你好，世界", "af_heart", 1.2, _cfg(tmp_path, {"model_id": "kokoro"}))
    assert err is None and audio == b"RIFF-audio-bytes" and ctype == "audio/wav"
    url, payload = client.posts[-1]
    assert url.endswith("/v1/audio/speech")
    assert payload["input"] == "你好，世界"
    assert payload["voice"] == "af_heart"
    assert payload["speed"] == 1.2
    assert payload["model"] == "kokoro"
    assert payload["response_format"] == "wav"


@pytest.mark.asyncio
async def test_synthesize_service_error_is_reported(tmp_path, monkeypatch):
    _stub(monkeypatch, _FakeResp(status=500, content=b"{}", ctype="application/json",
                                 body={"detail": "model not loaded"}))
    audio, _, err = await tts_service.synthesize("你好", None, None, _cfg(tmp_path))
    assert audio is None
    assert err["code"] == "tts_service_error"
    assert "model not loaded" in err["message"]


@pytest.mark.asyncio
async def test_synthesize_omits_voice_when_unset(tmp_path, monkeypatch):
    """没选音色＝不发声这个键（让服务用自己的默认音色）。

    audio.cpp 把空串当真音色名查，直接 500：「unknown Kokoro voice id: 」。发空串等于
    让所有人都静默退回系统语音。
    """
    client = _stub(monkeypatch, _FakeResp(content=b"RIFF-x"))
    await tts_service.synthesize("你好", None, None, _cfg(tmp_path))
    assert "voice" not in client.posts[-1][1]


@pytest.mark.asyncio
async def test_synthesize_truncates_to_max_chars(tmp_path, monkeypatch):
    client = _stub(monkeypatch, _FakeResp())
    await tts_service.synthesize("啊" * 50, None, None, _cfg(tmp_path, {"max_chars": 10}))
    assert client.posts[-1][1]["input"] == "啊" * 10


@pytest.mark.asyncio
async def test_list_voices_unions_all_models(tmp_path, monkeypatch):
    """下拉里是各模型音色的并集（本机：Kokoro 100 + v1.0 49 + 克隆 1）。"""
    monkeypatch.setattr(tts_service, "model_voice_routes",
                        lambda conf: {"女声001": ("kokoro", "女声001"),
                                      "女声-晓晓": ("kokoro_v10", "女声-晓晓"),
                                      "克隆·婷婷": ("indextts", "克隆·婷婷")})
    data = await tts_service.list_voices(_cfg(tmp_path))
    assert data["status"] == "success"
    assert data["voices"] == ["克隆·婷婷", "女声-晓晓", "女声001"]   # 按码点排序


def test_voice_routes_map_by_model(monkeypatch):
    calls = []

    def fake_fetch(url, conf, timeout=5.0):
        calls.append(url)
        if url.endswith("/v1/models"):
            return {"data": [{"id": "kokoro"}, {"id": "indextts"}]}
        if "model=kokoro" in url:
            return {"voices": ["女声001"]}
        if "model=indextts" in url:
            return {"voices": ["克隆·婷婷"]}
        return {"voices": []}

    monkeypatch.setattr(tts_service, "_fetch_json", fake_fetch)
    monkeypatch.setattr(tts_service, "_voice_routes", {"map": {}, "at": 0.0, "base": ""})
    routes = tts_service.model_voice_routes({"base_url": "http://127.0.0.1:7799"})
    assert routes["女声001"] == ("kokoro", "女声001")
    assert routes["克隆·婷婷"] == ("indextts", "克隆·婷婷")
    assert any("model=indextts" in u for u in calls), "每个模型都要单独问一次"


@pytest.mark.asyncio
async def test_synthesize_routes_voice_to_its_own_model(tmp_path, monkeypatch):
    """选了别的模型的音色 → 把请求发给那个模型（前端只发一个字符串）。"""
    client = _stub(monkeypatch, _FakeResp(content=b"RIFF-x"))
    monkeypatch.setattr(tts_service, "model_voice_routes",
                        lambda conf: {"克隆·婷婷": ("indextts", "克隆·婷婷")})
    await tts_service.synthesize("你好", "克隆·婷婷", None,
                                 _cfg(tmp_path, {"model_id": "kokoro_v10"}))
    payload = client.posts[-1][1]
    assert payload["model"] == "indextts"
    assert payload["voice"] == "克隆·婷婷"


@pytest.mark.asyncio
async def test_synthesize_keeps_config_model_for_unknown_voice(tmp_path, monkeypatch):
    client = _stub(monkeypatch, _FakeResp(content=b"RIFF-x"))
    monkeypatch.setattr(tts_service, "model_voice_routes", lambda conf: {})
    await tts_service.synthesize("你好", "zzz", None, _cfg(tmp_path, {"model_id": "kokoro_v10"}))
    assert client.posts[-1][1]["model"] == "kokoro_v10"


@pytest.mark.asyncio
async def test_list_voices_when_service_down(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_service, "probe_service", lambda conf: False)
    data = await tts_service.list_voices(_cfg(tmp_path))
    assert data["status"] == "error" and data["code"] == "tts_unavailable"


# ── 零样本克隆模型：参考音频必须能传下去 ──────────────────────────
@pytest.mark.asyncio
async def test_synthesize_passes_ref_audio_when_configured(tmp_path, monkeypatch):
    client = _stub(monkeypatch, _FakeResp(content=b"RIFF-x"))
    await tts_service.synthesize(
        "你好", None, None,
        _cfg(tmp_path, {"model_id": "IndexTTS-2", "ref_audio": "/tmp/ref.wav"}))
    sent = client.posts[-1][1]
    assert sent["ref_audio"] == "/tmp/ref.wav"
    assert sent["voice_ref"] == "/tmp/ref.wav"   # audio.cpp 的字段名，两边都发


@pytest.mark.asyncio
async def test_synthesize_omits_ref_audio_when_empty(tmp_path, monkeypatch):
    """有音色包的模型（Kokoro 等）不带这个键，别给服务塞空路径。"""
    client = _stub(monkeypatch, _FakeResp(content=b"RIFF-x"))
    await tts_service.synthesize("你好", None, None, _cfg(tmp_path))
    assert "ref_audio" not in client.posts[-1][1]
    assert "voice_ref" not in client.posts[-1][1]


# ── 200 + audio/* 里混 JSON 错误：不能当音频播出去 ────────────────
@pytest.mark.asyncio
async def test_synthesize_json_error_masked_as_audio_is_rejected(tmp_path, monkeypatch):
    _stub(monkeypatch, _FakeResp(
        content=b'{"error": {"message": "Must provide one of ref_audio", "type": "ValueError"}}',
        ctype="audio/wav"))
    audio, ctype, err = await tts_service.synthesize("你好", None, None, _cfg(tmp_path))
    assert audio is None and ctype is None
    assert err["code"] == "tts_service_error"
    assert "ref_audio" in err["message"]


def test_looks_like_json_only_for_object_bodies():
    assert tts_service._looks_like_json(b'  {"error": 1}')
    assert not tts_service._looks_like_json(b"RIFF\x00\x00\x00\x00WAVE")


def test_status_reports_enabled_and_availability(tmp_path, monkeypatch):
    monkeypatch.setattr(tts_service, "probe_service", lambda conf: True)
    st = tts_service.status(_cfg(tmp_path, {"model_id": "melotts"}))
    assert st == {"enabled": True, "available": True,
                  "base_url": tts_service.DEFAULT_TTS["base_url"],
                  "model_id": "melotts", "voice": "", "ref_audio": "", "speed": 1.0,
                  "timeout": tts_service.DEFAULT_TTS["timeout"]}
    monkeypatch.setattr(tts_service, "probe_service", lambda conf: False)
    st_off = tts_service.status(_cfg(tmp_path, {"enabled": False}))
    assert st_off["enabled"] is False and st_off["available"] is False


# ── 音高（变调后处理）────────────────────────────────────────────
def _sine_wav(path, freq=440.0, secs=1.0, sr=24000):
    import math, wave, struct
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr)))
                               for i in range(int(sr * secs))))


def test_wav_sample_rate_reads_header(tmp_path):
    p = tmp_path / "t.wav"; _sine_wav(p, sr=24000)
    assert tts_service._wav_sample_rate(p.read_bytes()) == 24000
    assert tts_service._wav_sample_rate(b"not a wav") == 0


def test_pitch_shift_is_noop_at_unity(tmp_path):
    p = tmp_path / "t.wav"; _sine_wav(p)
    body = p.read_bytes()
    assert tts_service._pitch_shift(body, 1.0) == body
    assert tts_service._pitch_shift(body, 1.004) == body      # 1% 以内不动


def test_pitch_shift_is_noop_without_ffmpeg(tmp_path, monkeypatch):
    """没装 ffmpeg 的机器上静默跳过 —— 朗读绝不能因为变调失败而变哑。"""
    p = tmp_path / "t.wav"; _sine_wav(p)
    body = p.read_bytes()
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: None)
    assert tts_service._pitch_shift(body, 1.15) == body


@pytest.mark.asyncio
async def test_synthesize_applies_configured_pitch(tmp_path, monkeypatch):
    calls = []
    _stub(monkeypatch, _FakeResp(content=b"RIFF-audio"))
    monkeypatch.setattr(tts_service, "_pitch_shift",
                        lambda audio, f: (calls.append(f), audio + b"!")[1])
    audio, _, err = await tts_service.synthesize(
        "你好", None, None, _cfg(tmp_path, {"pitch": 1.15}))
    assert err is None and calls == [1.15] and audio.endswith(b"!")


@pytest.mark.asyncio
async def test_synthesize_skips_pitch_when_not_configured(tmp_path, monkeypatch):
    called = []
    _stub(monkeypatch, _FakeResp(content=b"RIFF-audio"))
    monkeypatch.setattr(tts_service, "_pitch_shift", lambda a, f: called.append(f) or a)
    await tts_service.synthesize("你好", None, None, _cfg(tmp_path))
    assert called == []
