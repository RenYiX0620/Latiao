"""阶段 4：模型适配器层测试（model_adapters.py）。

覆盖 SSE 行解析的全变体：文本/推理（reasoning_content 双格式）/工具增量/
usage-only 块/finish_reason/[DONE]/坏 JSON/非 data 行；适配器 prepare 校验。
"""
import pytest

from model_adapters import Chunk, OpenAICompatAdapter, ParsedLine, ToolCallDelta


class TestParseLine:
    @pytest.mark.parametrize("line", [
        'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}',
        'data:{"choices":[{"delta":{"content":"hi"},"index":0}]}',   # 无空格变体
    ])
    def test_text_chunk(self, line):
        parsed = OpenAICompatAdapter.parse_sse_data_line(line)
        assert parsed.parse_error is None
        assert len(parsed.chunks) == 1
        assert parsed.chunks[0].kind == "text"
        assert parsed.chunks[0].text == "hi"

    def test_reasoning_both_keys(self):
        for key in ("reasoning", "reasoning_content"):
            parsed = OpenAICompatAdapter.parse_sse_data_line(
                f'data: {{"choices":[{{"delta":{{"{key}":"思考中"}},"index":0}}]}}'
            )
            assert parsed.chunks[0].reasoning == "思考中"

    def test_tool_call_delta(self):
        parsed = OpenAICompatAdapter.parse_sse_data_line(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            '"function":{"name":"run_cmd","arguments":"{\\"cmd\\"}"}}]},"index":0}]}'
        )
        assert parsed.chunks[0].kind == "tool_call"
        tc = parsed.chunks[0].tool_call
        assert isinstance(tc, ToolCallDelta)
        assert tc.index == 0 and tc.id == "c1"
        assert tc.name == "run_cmd" and tc.arguments == '{"cmd"}'

    def test_usage_only_chunk(self):
        parsed = OpenAICompatAdapter.parse_sse_data_line(
            'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}'
        )
        assert parsed.chunks[0].kind == "usage"
        assert parsed.chunks[0].usage["completion_tokens"] == 2

    def test_finish_reason(self):
        parsed = OpenAICompatAdapter.parse_sse_data_line(
            'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}'
        )
        assert parsed.chunks[0].kind == "finish"
        assert parsed.chunks[0].finish_reason == "stop"

    def test_text_and_finish_combined_line(self):
        parsed = OpenAICompatAdapter.parse_sse_data_line(
            'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop","index":0}]}'
        )
        assert [c.kind for c in parsed.chunks] == ["finish", "text"]
        assert parsed.chunks[1].text == "好"

    def test_done_marker(self):
        parsed = OpenAICompatAdapter.parse_sse_data_line("data: [DONE]")
        assert parsed.done is True and parsed.chunks == []

    def test_non_data_line_ignored(self):
        parsed = OpenAICompatAdapter.parse_sse_data_line(": heartbeat")
        assert parsed.chunks == [] and parsed.parse_error is None

    def test_bad_json_reported(self):
        parsed = OpenAICompatAdapter.parse_sse_data_line("data: {not json")
        assert parsed.parse_error is not None
        assert parsed.chunks == []

    def test_empty_delta(self):
        parsed = OpenAICompatAdapter.parse_sse_data_line(
            'data: {"choices":[{"delta":{},"index":0}]}'
        )
        assert parsed.chunks == []


class TestAdapter:
    def test_prepare_ok(self):
        prepared = OpenAICompatAdapter(endpoint="http://127.0.0.1:1234/v1", model="m").prepare()
        assert prepared is not None
        assert prepared.model == "m"

    def test_prepare_rejects_missing_model_or_bad_endpoint(self):
        assert OpenAICompatAdapter(endpoint="http://x/v1", model="").prepare() is None
        assert OpenAICompatAdapter(endpoint="not-url", model="m").prepare() is None


class TestLoopDeltaShape:
    """两循环共用的 _parse_delta_line 语义（旧 json.loads 块的逐行为等价）。"""

    @staticmethod
    def _parse(line):
        from agent_loop import _parse_delta_line
        return _parse_delta_line(line)

    def test_done(self):
        assert self._parse("data: [DONE]") == (True, None)

    def test_usage_only_skipped(self):
        done, delta = self._parse('data: {"choices":[],"usage":{"a":1}}')
        assert (done, delta) == (False, None)

    def test_bad_json_skipped(self):
        done, delta = self._parse("data: {broken")
        assert (done, delta) == (False, None)

    def test_text_delta(self):
        done, delta = self._parse('data: {"choices":[{"delta":{"content":"你好"}}]}')
        assert (done, delta) == (False, {"content": "你好"})

    def test_reasoning_content_normalized(self):
        """本地循环旧读键（reasoning or reasoning_content）——统一映射为 reasoning。"""
        done, delta = self._parse(
            'data: {"choices":[{"delta":{"reasoning_content":"思考"}}]}'
        )
        assert (done, delta) == (False, {"reasoning": "思考"})

    def test_tool_call_rebuilt_old_shape(self):
        done, delta = self._parse(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            '"function":{"name":"ls","arguments":"{}"}}]}}]}'
        )
        assert done is False
        tc = delta["tool_calls"][0]
        assert tc["index"] == 0 and tc["id"] == "c1"
        assert tc["function"] == {"name": "ls", "arguments": "{}"}

    def test_empty_delta_counts_like_before(self):
        assert self._parse('data: {"choices":[{"delta":{}}]}') == (False, {})
