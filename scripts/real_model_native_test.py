"""真模型自测：Latiao 完整本地循环 × 真实 mlx 引擎（Qwen3.8-27B）。

场景 A：原生 function calling 工具轮次（列出目录 → 工具执行 → 中文总结交付）
场景 B：闲聊快车道（关思考，单轮直达）
前置：mlx_lm.server 已在 1236 端口加载 Qwen3.8-27B-MLX-4bit。
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, "/Users/langzuxiang/Desktop/Agent/local-ai-os/sidecar")
sys.path.insert(0, "/Users/langzuxiang/Desktop/Agent/local-ai-os")

import agent_loop
from agent_loop import _local_agent_loop_stream, _clear_session_cancel

MODEL = "/Users/langzuxiang/.lmstudio/models/lmstudio-community/Qwen3.8-27B-MLX-4bit"
API_URL = "http://127.0.0.1:1236/v1/chat/completions"
HEADERS = {"Authorization": "Bearer x"}


class _RealEngineStub:
    """真引擎已独立运行；这里只补 local_llm._engine 的忙注册接口。"""
    current_model_id = MODEL
    server_status = "running"
    _auto_reloading = False
    _explicit_stop = False
    _active_backend = "mlx"
    server_port = 1236

    def mark_engine_busy(self, *a, **k): pass
    def mark_engine_idle(self): pass
    def mark_stream_enter(self): pass
    def mark_stream_exit(self): pass
    def _kill_port(self, *a): pass
    def _request_reload(self, *a): return False


async def run_case(title, messages, timeout_s):
    import local_llm
    old = local_llm._engine
    local_llm._engine = _RealEngineStub()
    agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = True
    sid = f"real-{int(time.time()*1000)}"
    _clear_session_cancel(sid)
    t0 = time.monotonic()
    rounds, tool_calls, texts = 0, 0, []
    try:
        async for ev in _local_agent_loop_stream(
                messages, MODEL, API_URL, HEADERS,
                session_id=sid, access_mode="full"):
            if ev.get("event") == "round_start":
                rounds += 1
                print(f"  [round {ev['iteration']}] {time.monotonic()-t0:.1f}s")
            elif ev.get("event") == "tool_start":
                tool_calls += 1
                print(f"  [tool] {ev.get('tool')} {json.dumps(ev.get('args', {}), ensure_ascii=False)[:60]} {time.monotonic()-t0:.1f}s")
            elif ev.get("event") == "tool_end":
                print(f"  [tool done] {(ev.get('result') or '')[:60]!r}")
            elif ev.get("content"):
                texts.append(str(ev["content"]))
        dur = time.monotonic() - t0
        body = "".join(texts).strip()
        print(f"  => {title}: {dur:.1f}s, {rounds} 轮, {tool_calls} 次工具, 正文 {len(body)} 字")
        print(f"  正文预览: {body[:150]!r}")
        return dur, rounds, tool_calls, len(body)
    finally:
        local_llm._engine = old
        agent_loop._LOCAL_NATIVE_TOOLS_OVERRIDE = None


async def main():
    print("== 场景 A：原生工具轮次（列出目录）==")
    a = await run_case("A 工具轮次", [
        {"role": "system", "content": "你是本机助手，始终用简体中文回复。"},
        {"role": "user", "content": "列出 /tmp 目录下有哪些文件，然后告诉我总共几个。"},
    ], timeout_s=300)
    print("== 场景 B：闲聊快车道 ==")
    b = await run_case("B 闲聊快车道", [
        {"role": "system", "content": "你是本机助手，始终用简体中文回复。"},
        {"role": "user", "content": "测试"},
    ], timeout_s=120)
    print("\n== 结论 ==")
    print(f"A: {a[0]:.1f}s {a[1]}轮 {a[2]}工具 {a[3]}字  （基线：围栏格式同任务分钟级）")
    print(f"B: {b[0]:.1f}s {b[1]}轮 {b[2]}工具 {b[3]}字  （基线：带思考 ~7s）")

asyncio.run(main())
