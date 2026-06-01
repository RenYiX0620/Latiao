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
- `src/` — React frontend (App.tsx is the main component, ~1000 lines)
- `src-tauri/` — Rust backend (Tauri commands proxy to sidecar)
- `sidecar/main.py` — Python FastAPI: agent loop, tool execution, SSE streaming
- Frontend manages all session state (localStorage); sidecar is stateless
- Agent loop: SSE streaming with `tool_confirm`/`tool_start`/`tool_end` events
- Tool permissions: `safe` = auto-execute, `confirm` = user must approve
- Progress persisted to `~/.local-ai-os/PROGRESS.md`

## When editing sidecar/main.py
- The agent loop uses `asyncio.Event` for tool confirmation — don't break the async flow.
- `TOOLS`, `TOOL_DISPATCH`, `TOOL_PERMISSIONS` must stay in sync.
- New tools need: function definition + dispatch entry + permission level.
- The SSE event protocol is: `content` for tokens, `tool_confirm`/`tool_start`/`tool_end` for tool lifecycle, `[DONE]` for stream end.

## When editing src/App.tsx
- `buildApiMessages()` skips tool messages (role "tool" / type "tool_call").
- `streamChat()` SSE parser must handle all event types.
- ToolCallBubble receives `onConfirm` callback for permission prompts.
- Session state is persisted to localStorage key `local_ai_os_sessions`.
