# Latiao Architecture

## Project Overview

Latiao is a Tauri 2 desktop AI agent for macOS (Apple Silicon). It combines a Rust backend, React 19 frontend, and Python FastAPI sidecar. The sidecar runs the actual agent loop.

```
User → React UI → Tauri Rust → HTTP proxy → Python FastAPI → LLM API
                                      ↓
                              macOS Keychain (secrets)
```

---

## Technology Stack

| Layer      | Technology              | Role                               |
|------------|-------------------------|------------------------------------|
| Shell      | Tauri 2 (Rust)          | Desktop window, IPC, sidecar mgmt   |
| Frontend   | React 19 + TypeScript   | Chat UI, tool config, multi-agent   |
| Backend    | Python FastAPI          | Agent loop, tool execution, memory  |
| LLM        | MLX / llama.cpp / HTTP  | Local + cloud model inference       |
| Storage    | SQLite                  | Memory, preferences, progress       |

---

## Agent Loop Flow

```
┌─────────────────────────────────────────────────────────┐
│                    chat_completion()                      │
│  POST /v1/chat/completions                               │
│                                                          │
│  1. Parse request body                                   │
│  2. 组装上下文：identity/persona + 工具表 + 会话历史      │
│     （agent_loop._build_chat_messages）                   │
│  3. 选路：本地 or 云端 → 都交给同一个 ThinAgentLoop       │
│     （agent/loop.py；本地经 transport 闸门，容量=引擎槽位）│
│  4. Stream events via SSE to frontend                    │
└─────────────────────────────────────────────────────────┘

                    ▼

┌─────────────────────────────────────────────────────────┐
│        ThinAgentLoop.run()（agent/loop.py，唯一循环）     │
│                                                          │
│  while 未达步数上限:                                      │
│    ├─ 组请求体（含 max_tokens/思考开关，按引擎类型分流）  │
│    ├─ 流式采样 → 实时 yield content / reasoning           │
│    ├─ 解析工具调用：原生 delta.tool_calls                 │
│    │   └─ 或文本式方言（agent/parsing.py 的               │
│    │      _parse_native/_parse_prompt + 半截标记清洗）     │
│    ├─ 执行工具 → _handle_tool_execution()（含子代理闸门/  │
│    │   权限档判定/确认流）                                │
│    ├─ 停滞检测与去重（同参重复、纯文本空转）              │
│    └─ 轮次/收口判定（cron 路径另有"正文可交付即收口"）    │
│  Yield "[DONE]"                                          │
└─────────────────────────────────────────────────────────┘

注：2026-09-23 校正 —— 此前本文档描述的 `_agent_loop_stream()` /
`_local_agent_loop_stream()`（云/本地双循环）已随薄循环重构删除，代码里 0 处存在。
```

---

## SSE Event Protocol

The sidecar communicates with the frontend via Server-Sent Events. Each event is a JSON object with an `event` field:

| Event          | Direction | Meaning                            |
|----------------|-----------|------------------------------------|
| `content`      | → UI      | Streaming text token               |
| `tool_confirm` | → UI      | Tool needs user approval           |
| `tool_start`   | → UI      | Tool execution begins              |
| `tool_end`     | → UI      | Tool execution finished + result   |
| `[DONE]`       | → UI      | Stream complete                    |
| `confirm`      | ← UI      | User approved a tool (POST /v1/confirm) |

---

## Tool System

### Plugin Architecture

```
sidecar/tool_system.py          ← Plugin loader (scans plugins/)
sidecar/plugins/                ← Individual tool plugins
  ├── read_file.py              ← NAME, DEFINITION, PERMISSION, execute()
  ├── write_file.py
  ├── list_dir.py
  ├── run_cmd.py
  ├── open_app.py
  ├── open_folder.py
  ├── search_files.py
  └── tavily_search.py

sidecar/main.py (Section 4)     ← Fallback implementations (used when plugins/ is empty)
```

### How to Add a New Tool

1. **Create** `sidecar/plugins/my_tool.py`:
```python
NAME = "my_tool"
PERMISSION = "safe"  # or "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "What this tool does",
        "parameters": {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "..."}
            },
            "required": ["param1"]
        }
    }
}

def execute(args: dict) -> str:
    return f"Result: {args['param1']}"
```

2. **Restart** the sidecar — plugins are scanned at startup.

3. **Verify** in the UI under Tools tab.

---

## Permission System

Tools have two permission levels:

| Level   | Behavior                                      |
|---------|-----------------------------------------------|
| `safe`  | Auto-execute, no user confirmation needed      |
| `confirm`| Pause → send `tool_confirm` to UI → wait for user approval (120s timeout) |

Permission is determined by:
1. Plugin's `PERMISSION` constant
2. Fallback dict `TOOL_PERMISSIONS` in main.py
3. Per-session overrides from user config

---

## Project File Map

```
local-ai-os/
├── sidecar/                         Python sidecar
│   ├── main.py                      FastAPI app + agent loops + tools (10 sections)
│   ├── tool_system.py               Plugin loader
│   ├── local_llm.py                 Local LLM engine (MLX, llama.cpp)
│   ├── memory.py                    SQLite + TF-IDF memory
│   ├── identity.py                  Agent personality system
│   ├── config.py                    Path constants
│   ├── db.py                        Database helpers
│   ├── progress.py                  Progress tracking + cron
│   ├── plugins/                     Individual tool implementations
│   ├── skills/                      SKILL.md skill definitions
│   ├── agents/                      Agent identity/prompt files (*.txt)
│   └── tests/                       Test files
├── src/                             React 19 frontend
│   ├── App.tsx                      Main app (sessions, chat, agent routing)
│   ├── components/
│   │   ├── ChatView.tsx             Chat messages + ReactMarkdown rendering
│   │   ├── ToolCallBubble.tsx       Tool call display + confirm buttons
│   │   ├── AgentView.tsx            Multi-agent selection
│   │   ├── ModelsView.tsx           Local + cloud model management
│   │   ├── ToolsView.tsx            Tool catalog view
│   │   ├── SettingsView.tsx         API keys, preferences
│   │   ├── CronView.tsx             Scheduled task management
│   │   └── LogsView.tsx             Sidecar log viewer
│   ├── hooks/                       React hooks (useSessions, useCronJobs)
│   ├── i18n/                        Internationalization
│   ├── utils/                       API client, crypto
│   └── types.ts                     TypeScript type definitions
├── src-tauri/                       Rust backend
│   ├── src/main.rs                  Tauri commands + sidecar management
│   ├── tauri.conf.json              App config (window, bundle, updater)
│   └── Cargo.toml                   Rust dependencies
├── scripts/
│   ├── setup-portable-python.sh     Downloads python-build-standalone for bundling
│   └── release.sh                   Build + sign + publish to GitHub Releases
├── package.json                     npm scripts (dev, build, deploy, release)
└── README.md                        Project overview (English + 中文)
```

---

## Key Design Decisions

1. **Stateless sidecar**: Frontend manages all session state (localStorage). Sidecar receives complete message history with every request. This simplifies restart/error recovery.

2. **Portable Python**: Production .app bundles python-build-standalone (Python 3.11). Users don't need Python installed. See `scripts/setup-portable-python.sh`.

3. **Plugin-first tools**: Tools are defined as plugins in `sidecar/plugins/`. The fallback implementations in main.py Section 4 only activate when the plugins directory is empty (first-run seeding).

4. **Dual agent loops**: Cloud models get native function calling. Local models get prompt-based tool calling (model-agnostic). Both loops share the same tool execution and verification pipeline.

5. **SSE streaming**: All agent output streams to the UI in real-time via SSE events. No polling. No WebSocket overhead.
