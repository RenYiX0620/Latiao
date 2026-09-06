# CLAUDE.md — 辣条 Latiao

## Project
Tauri 2 (Rust) + React 19 (TypeScript) + Python FastAPI sidecar desktop AI agent.
The sidecar runs a local agent loop with OpenAI Function Calling tools.

## 🛑 HARD RULE: Ask Before Any Write
- **NEVER** call Write, Edit, or any file-modifying Bash command without explicit user approval first.
- When a user says "fix X" or "deploy", you must first: explain what you will do, why, and which files. Then **wait** for explicit confirmation ("yes", "go ahead", "执行") before touching any file.
- A one-word command ("修复", "部署") is NOT blanket approval — pause and confirm scope.
- This rule overrides all others. Violating it = breaking user trust.

## Think Before Coding
- When requirements are ambiguous, ask — don't silently pick an interpretation.
- If there's a simpler approach than what was requested, say so.
- Stop and ask when genuinely unclear.

## Simplicity First
- Don't add features not in the requirements.
- Don't add abstraction for "future use."
- One-shot operations don't need helper functions.
- Similar code repeated twice is fine; abstract on the third occurrence.

## Surgical Changes
- Every changed line must trace back to the user's request.
- Fixing bug A does not mean refactoring file B or renaming variable C.
- Before committing, verify: can you explain why each file was touched?

## Goal-Driven Execution
- Before fixing a bug: reproduce it first.
- After making changes: run the verification command.
- If verification fails, report the command, the failure, and the unverified risk.

## Commands
```bash
# Frontend type-check
npx tsc --noEmit

# Frontend build
npx vite build

# Rust build
cd src-tauri && cargo build

# Start sidecar
cd sidecar && python3 main.py

# Full Tauri dev
npx tauri dev
```

## Architecture
- `src/` — React frontend (App.tsx is the main component, ~1700 lines; ChatView.tsx ~800)
- `src-tauri/` — Rust backend (Tauri commands proxy to sidecar)
- `sidecar/` — Python FastAPI sidecar: agent loops in `agent_loop.py`（云/本地双循环）+ `agent_loop_v2.py`（灰度统一循环）, routing/API in `api_routes.py`, tools in `tool_executor.py`/`tool_system.py`, engine in `local_llm.py`; main.py is the FastAPI app + facade
- Frontend manages all session state (localStorage); sidecar is stateless
- Agent loop: SSE streaming with `tool_confirm`/`tool_start`/`tool_end` events
- Tool permissions: `safe` = auto-execute, `confirm` = user must approve
- Progress persisted to `~/.local-ai-os/PROGRESS.md`

## When editing the sidecar agent layer
- The agent loop uses `asyncio.Event` for tool confirmation — don't break the async flow.
- `TOOLS`, `TOOL_DISPATCH`, `TOOL_PERMISSIONS` must stay in sync.
- New tools need: function definition + dispatch entry + permission level.
- 路由策略：未选模型一律本地引擎；云端只走显式选择（自动路由机制已删除，哨兵测试 test_no_auto_route_to_cloud）。
- The SSE event protocol is: `content` for tokens, `tool_confirm`/`tool_start`/`tool_end` for tool lifecycle, `[DONE]` for stream end.

## When editing src/App.tsx
- `buildApiMessages()` skips tool messages (role "tool" / type "tool_call").
- `streamChat()` SSE parser must handle all event types.
- ToolCallBubble receives `onConfirm` callback for permission prompts.
- Session state is persisted to localStorage key `local_ai_os_sessions`.
