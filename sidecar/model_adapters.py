"""模型适配器层（阶段 4）——Chunk 协议 + OpenAI 兼容适配器（dsh LlmAdapter 概念的移植）。

对照：dsh `LlmAdapter`（llm/src/index.ts:187）最小接口 = stream() + 可选
prepare()；Latiao 全仓库没有适配器抽象——agent_loop 两个巨型循环各自内联
拼 dict 并手写 SSE 解析（云端/本地两套，第三处在 cron）。本模块提供协议层：

- `Chunk`：跨厂商规范化的单块流数据（text / reasoning / tool_call / usage /
  finish）；dsh 对应 StreamChunk + BlockAssembler 的输入。
- `parse_sse_data_line()`：OpenAI 兼容 `data: {...}` 行的规范化解析——两循环
  现各自内联 `json.loads + choices[0].get("delta")`，此函数收敛为唯一实现
  （增量收敛的第一步；请求体拼装的收敛属于 2b 循环合并，见模块尾部的接缝表）。

边界（诚实声明）：本模块不做"把请求体拼装收敛进 adapter"（agent_loop
内联 dict 的收敛属于阶段 2b 循环合并的一部分）——本模块是协议/解析层，
可独立测试，接缝已在文档中标注。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

logger = logging.getLogger("latiao-sidecar")

ChunkKind = Literal["text", "reasoning", "tool_call", "usage", "finish"]


@dataclass(frozen=True)
class ToolCallDelta:
    """OpenAI 风格工具调用增量（tool_calls 数组元素）。"""

    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True)
class Chunk:
    """跨厂商规范化的流式块。同一行可能同时带多项（如 text+finish），
    按 kind 拆分产出（解析器对每行最多返回一个 chunk + 一个 finish 标记）。"""

    kind: ChunkKind
    text: str | None = None
    reasoning: str | None = None
    tool_call: ToolCallDelta | None = None
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class ParsedLine:
    chunks: list[Chunk]
    done: bool = False
    parse_error: str | None = None


class OpenAICompatAdapter:
    """OpenAI 兼容 SSE 适配器（云/本地引擎同一格式，一个实现覆盖两条路径）。

    prepare() 只做配置形态校验（不联网、不查目录），后续 2b 合并时
    stream() 才真正发请求——届时本类成为两个循环的公共数据面。
    """

    def __init__(self, *, endpoint: str = "", api_key: str = "", model: str = ""):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model

    def prepare(self) -> "PreparedCall | None":
        """配置形态校验：endpoint 是 http(s) 且 model 非空才可调用。
        返回 None 表示没有可用准备态（调用方应报错而非静默回退）。"""
        if not self.model:
            return None
        if not self.endpoint.lower().startswith(("http://", "https://")):
            return None
        return PreparedCall(adapter=self, model=self.model, endpoint=self.endpoint)

    @staticmethod
    def parse_sse_data_line(line: str) -> ParsedLine:
        """解析一行 SSE（`data: {...}` 或 `data: [DONE]`）。

        无论两循环怎么用，本函数是唯一实现——增量收敛点（见模块头）。
        非 `data:` 行（心跳/空行）返回空 ParsedLine（调用方按自身语义处理）。
        """
        if not line:
            return ParsedLine(chunks=[])
        if line.startswith("data: "):
            data_str = line[6:]
        elif line.startswith("data:"):
            data_str = line[5:]
        else:
            return ParsedLine(chunks=[])
        if data_str.strip() == "[DONE]":
            return ParsedLine(chunks=[], done=True)
        try:
            event = json.loads(data_str)
        except (TypeError, ValueError) as exc:
            return ParsedLine(chunks=[], parse_error=str(exc))
        chunks: list[Chunk] = []
        choices = event.get("choices") or []
        if not choices:
            usage = event.get("usage")
            if usage:
                chunks.append(Chunk(kind="usage", usage=usage))
            return ParsedLine(chunks=chunks)
        choice = choices[0]
        delta = choice.get("delta", {}) or {}
        finish = choice.get("finish_reason")
        if finish:
            chunks.append(Chunk(kind="finish", finish_reason=finish))
        _append_delta_chunks(delta, chunks)
        return ParsedLine(chunks=chunks)


def _append_delta_chunks(delta: dict[str, Any], chunks: list[Chunk]) -> None:
    """delta 拆分为规范化 chunk：content / reasoning / tool_calls。"""
    text = delta.get("content")
    if isinstance(text, str) and text:
        chunks.append(Chunk(kind="text", text=text))
    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        chunks.append(Chunk(kind="reasoning", reasoning=reasoning))
    for tc in delta.get("tool_calls", []) or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {}) or {}
        chunks.append(Chunk(kind="tool_call", tool_call=ToolCallDelta(
            index=int(tc.get("index", 0) or 0),
            id=str(tc.get("id", "")),
            name=str(fn.get("name", "")),
            arguments=str(fn.get("arguments", "")),
        )))


@dataclass(frozen=True)
class PreparedCall:
    """prepare() 的结果：绑定适配器的调用准备态（对齐 dsh PreparedLlmCall）。"""

    adapter: OpenAICompatAdapter
    model: str
    endpoint: str
