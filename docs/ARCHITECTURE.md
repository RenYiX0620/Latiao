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
│  2. Match skill (keyword → SKILL.md)                     │
│  3. Build messages (system prompt + history + skill)     │
│  4. Choose model route:                                  │
│     ├─ Local model  → _local_agent_loop_stream()         │
│     └─ Cloud API    → _agent_loop_stream()               │
│  5. Stream events via SSE to frontend                    │
└─────────────────────────────────────────────────────────┘

                    ▼

┌─────────────────────────────────────────────────────────┐
│              _agent_loop_stream() (Cloud)                 │
│                                                          │
│  while iteration < 50:                                   │
│    ├─ Call LLM API with tools definition                 │
│    ├─ Stream tokens → yield SSE "content" events         │
│    ├─ Detect tool_calls in response:                     │
│    │   ├─ OpenAI native format (delta.tool_calls)        │
│    │   └─ Text-embedded format (parse_prompt_tool_calls) │
│    ├─ Execute tools → _handle_tool_execution()           │
│    ├─ Check stagnation (repeated calls)                  │
│    └─ Nudge if model stalls (text-only streaks)          │
│  End                                                     │
│  Yield "[DONE]"                                          │
└─────────────────────────────────────────────────────────┘
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
│   │   ├── SkillsView.tsx           Skill enable/disable UI
│   │   ├── ToolsView.tsx            Tool catalog view
│   │   ├── SettingsView.tsx         API keys, preferences
│   │   ├── CronView.tsx             Scheduled task management
│   │   └── LogsView.tsx             Sidecar log viewer
│   ├── hooks/                       React hooks (useSessions, useSkills, etc.)
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
