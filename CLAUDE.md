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

## Language (user preference, 2026-09-06)
- Commit messages and GitHub release notes MUST be written in English.
- This applies to every future push; do not switch back to Chinese.

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
- `src/` — React frontend (App.tsx is the main component, ~2100 lines; ChatView.tsx ~800)
- `src-tauri/` — Rust backend (Tauri commands proxy to sidecar)
- `sidecar/` — Python FastAPI sidecar. **真实布局（2026-09-23 校正，此前文档描述的 `agent_loop_v2.py` 已不存在）**：
  - `agent/loop.py` — `ThinAgentLoop`，**唯一的 agent 循环**（云/本地共用一个，注释自称"薄循环"）
  - `agent_loop.py` — **枢纽**（不是循环）：工具注册表与执行、工具确认、PROGRESS 记录、`_build_chat_messages` 上下文组装、MCP 加载、计划/反思、自动验证、`_resolve_api_target`
  - `agent/` 其余模块 — `transport.py`（本地引擎闸门/流）、`parsing.py`（工具调用方言解析与清洗）、`context.py`（权限档/工具过滤）、`gates.py`、`subagent.py`
  - `api_routes.py`（HTTP）、`tool_executor.py`/`tool_system.py`（工具与插件）、`local_llm.py`（引擎启动/配置）
  - 注意循环依赖靠惰性 import 硬扛（`agent/loop.py` 内 `from agent_loop import …`、`main.py` 末尾 `import api_routes` 反向注册路由）——改这两处之前先读注释
  - 跑测试：`pytest sidecar/tests`（仓库根）或 `cd sidecar && pytest tests` 结果一致（`sidecar/pytest.ini` 固定 rootdir/pythonpath）
- Frontend manages all session state (localStorage); sidecar is stateless
- Agent loop: SSE streaming with `tool_confirm`/`tool_start`/`tool_end` events
- Tool permissions: `safe` = auto-execute, `confirm` = user must approve
- Progress persisted to `~/.local-ai-os/PROGRESS.<session>.md`（按会话，注入回提示词的只有本会话） + 共享 `PROGRESS.md`（read_file 启动协议用）

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

## 发版流程（2026-09-25 起固定）

1. 升版本（package.json / src-tauri/tauri.conf.json / Cargo.toml / Cargo.lock 四处一致，`python3 scripts/check_versions.py` 校验）；
2. 提交 + 打 tag + 推送（`git push origin main && git push origin v<版本>`；origin 一次推 GitHub 与 Gitee）；
3. 等 CI 出包，核产物齐全与 `latest.json` 指向；
4. **补发布说明（新步骤）**：写一份"用户视角"的说明 → `gh release edit v<版本> --notes-file <文件>` 写进 Release body，同时 `python3 scripts/changelog_add.py <版本> <文件> [标题]` 追加进 `CHANGELOG.md`（版本已存在会被拒绝，防重复）。
