# 🌶️ Latiao — Your Local AI Agent

> **An AI agent that lives on your Mac. No cloud, no data leaks, your own models, your rules.**

<p align="center">
  <img src="assets/screenshot.png" alt="Latiao Screenshot" width="700">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://tauri.app"><img src="https://img.shields.io/badge/Tauri-2.0-blue" alt="Tauri 2"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.11-brightgreen" alt="Python 3.11"></a>
  <a href="https://github.com/RenYiX0620/Latiao/releases"><img src="https://img.shields.io/github/v/release/RenYiX0620/Latiao" alt="Latest Release"></a>
  <a href="https://github.com/RenYiX0620/Latiao/releases"><img src="https://img.shields.io/github/downloads/RenYiX0620/Latiao/total" alt="Downloads"></a>
</p>

---

Latiao is a desktop AI agent built with Tauri + React + Python FastAPI. It autonomously executes tasks on your computer — **read and write files, run shell commands, search codebases, open apps, manage projects** — all while keeping your data completely private and local.

## What Can It Do?

| Capability | Description |
|------------|-------------|
| 📂 **File Operations** | Read, write, search, and organize files across your filesystem |
| 💻 **Command Execution** | Run shell commands, scripts, and development toolchains |
| 🔍 **Code Intelligence** | Analyze, review, debug, refactor, and generate code |
| 🧠 **Multi-Model** | Local models (MLX / llama.cpp) or cloud APIs (OpenAI / DeepSeek / Anthropic) — you choose |
| 🤖 **Multi-Agent** | Specialized sub-agents for code review, debugging, documentation, and translation |
| 🧩 **Skills System** | Extensible SKILL.md plugins — load domain knowledge on demand |
| 💾 **Persistent Memory** | SQLite + TF-IDF semantic search — remembers across sessions |
| ✅ **Self-Verification** | Auto-validates its own work (re-reads files, runs ESLint, Python syntax check, TypeScript type-check) |
| ⏰ **Scheduled Tasks** | Cron-style automation — recurring jobs on your schedule |
| 🌐 **Multilingual** | UI in English, 中文, 日本語, Русский |

## How It Works

```
You: "Fix all lint errors in src/"       Latiao:
                                            ├─ Reads every file in src/
                                            ├─ Runs ESLint on each
                                            ├─ Applies fixes
                                            ├─ Re-runs ESLint to verify
                                            ├─ Reports: "Fixed 12 errors across 5 files ✅"
                                            └─ All data stays on your machine
```

The agent loop streams its thinking in real-time via SSE. For sensitive operations (file writes, command execution), it asks for your confirmation first. You're always in control.

## 📥 Install (macOS)

> ⚠️ **Apple Silicon only** (M1 / M2 / M3 / M4 / M5). Intel Mac and Windows are not yet supported.

Download the latest `.dmg` from [GitHub Releases](https://github.com/RenYiX0620/Latiao/releases):

1. Double-click the `.dmg` to mount it
2. Drag `Latiao.app` into your `Applications` folder
3. Double-click to launch

**That's it. No Python, no Node.js, no setup required. Download and run.**

## 🚀 Development

For those who want to build from source or contribute.

### Prerequisites

- macOS (Apple Silicon)
- Node.js 20+
- Python 3.10+
- Rust toolchain

### Quick Start

```bash
git clone https://github.com/RenYiX0620/Latiao.git
cd Latiao

# Frontend
npm install

# Python sidecar
cd sidecar
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd ..

# Launch in dev mode
npm run tauri dev
```

### Production Build

```bash
# One-command build: portable Python setup → compile → bundle → copy to Desktop
npm run deploy
```

Output:
- `Latiao.app` → `src-tauri/target/release/bundle/macos/`
- `Latiao_*.dmg` → `src-tauri/target/release/bundle/dmg/`

### Release

```bash
npm run release -- 0.2.0
```

Builds, signs, and publishes a new GitHub Release with auto-update support.

## 🏗️ Architecture

```
┌──────────────────────────────────────────┐
│         Tauri Desktop Shell               │
│  ┌────────────┐  ┌────────────────────┐   │
│  │  React 19  │  │  Rust Backend      │   │
│  │  (UI)      │  │  (Commands, Proxy) │   │
│  └─────┬──────┘  └─────────┬──────────┘   │
│        │                   │              │
└────────┼───────────────────┼──────────────┘
         │                   │
         ▼                   ▼
┌──────────────────────────────────────────┐
│     Python Sidecar (FastAPI + SSE)        │
│  ┌────────────────────────────────────┐   │
│  │        Agent Loop                  │   │
│  │  ├─ Streams tokens via SSE         │   │
│  │  ├─ OpenAI-compatible tool calling │   │
│  │  ├─ Permission system (safe/confirm)│   │
│  │  ├─ Self-verification pipeline     │   │
│  │  └─ Memory store (SQLite)          │   │
│  ├────────────────────────────────────┤   │
│  │      Local LLM Engine              │   │
│  │  ├─ MLX (Apple Silicon native)     │   │
│  │  ├─ llama.cpp                      │   │
│  │  └─ Model download & management    │   │
│  └────────────────────────────────────┘   │
└──────────────────────────────────────────┘
```

## 🧩 Skills System

Skills are the core extension mechanism. Each skill is a `SKILL.md` file under `sidecar/skills/` — Latiao loads them progressively based on context.

### Built-in Skills

| Skill | What it teaches Latiao |
|-------|----------------------|
| `code-review` | Code review methodology & security analysis patterns |
| `git-workflow` | Git conventions and commit best practices |
| `python-fastapi` | Python FastAPI idioms and common pitfalls |
| `typescript-react` | TypeScript React patterns and coding standards |

### Creating a Custom Skill

```markdown
# sidecar/skills/my-skill.md
---
name: my-skill
description: What this skill teaches the agent
---

## Rules
1. First rule the agent should follow
2. Second rule

## Patterns
- Pattern A → do X
- Pattern B → do Y

## Exit Criteria
- Conditions that signal task completion
```

Drop it in `sidecar/skills/` and Latiao picks it up automatically.

## 📄 License

MIT — free to use, modify, and distribute.

---

---

# 🌶️ 辣条 Latiao — 你的本地 AI 智能助手

> **跑在你 Mac 上的 AI 助手。不上传数据、不偷代码、用你自己的模型、听你自己的规则。**

Latiao（辣条）是一个桌面 AI Agent 应用，基于 Tauri + React + Python FastAPI 构建。它能像人一样在你的电脑上自主工作——**读写文件、执行命令、搜索代码、打开应用、管理项目**。所有数据都在本地，完全隐私。

## 它能做什么？

| 能力 | 说明 |
|------|------|
| 📂 **文件操作** | 读取、写入、搜索、管理你文件系统中的文件 |
| 💻 **命令执行** | 运行 Shell 命令、脚本、开发工具链 |
| 🔍 **代码智能** | 分析、审查、调试、重构、生成代码 |
| 🧠 **多模型** | 本地模型（MLX / llama.cpp）或云端 API（OpenAI / DeepSeek / Anthropic）自由切换 |
| 🤖 **多 Agent 协作** | 内置代码审查员、调试专家、文档生成器、翻译助手 |
| 🧩 **技能系统** | 可扩展的 SKILL.md 插件，按需加载领域知识 |
| 💾 **持久记忆** | SQLite + TF-IDF 语义搜索，跨会话记住你的偏好和上下文 |
| ✅ **自验证** | 自动检查自己的工作（回读文件、运行 ESLint、Python 语法、TypeScript 类型检查） |
| ⏰ **定时任务** | Cron 风格的定时自动化 |
| 🌐 **多语言** | 界面支持 English / 中文 / 日本語 / Русский |

## 它是怎么工作的？

```
你："帮我把 src/ 里所有 lint 错误修了"      Latiao：
                                               ├─ 逐个读取 src/ 下的文件
                                               ├─ 对每个文件运行 ESLint
                                               ├─ 自动应用修复
                                               ├─ 重新运行 ESLint 确认修复结果
                                               ├─ 汇报："已修复 5 个文件共 12 个错误 ✅"
                                               └─ 所有操作都在你的机器上完成
```

Agent 通过 SSE 实时流式输出思考和执行过程。对于敏感操作（写文件、执行命令），会先征求你的确认。你始终拥有最终控制权。

## 📥 下载安装（macOS）

> ⚠️ **仅支持 Apple Silicon Mac**（M1 / M2 / M3 / M4 / M5）。Intel Mac 和 Windows 暂不支持。

从 [GitHub Releases](https://github.com/RenYiX0620/Latiao/releases) 下载最新 `.dmg`：

1. 双击 `.dmg` 挂载
2. 把 `Latiao.app` 拖入 `Applications` 文件夹
3. 双击打开

**不需要装 Python、Node.js 或任何依赖。下载即用。**

## 🚀 开发者指南

从源码构建和开发 Latiao。

### 环境要求

- macOS（Apple Silicon）
- Node.js 20+
- Python 3.10+
- Rust 工具链

### 本地开发

```bash
git clone https://github.com/RenYiX0620/Latiao.git
cd Latiao

# 前端
npm install

# Python sidecar
cd sidecar
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd ..

# 启动开发模式
npm run tauri dev
```

### 生产构建

```bash
# 一键构建：便携 Python → 编译 → 打包 → 复制到桌面
npm run deploy
```

### 发布新版本

```bash
npm run release -- 0.2.0
```

## 🏗️ 架构

```
┌──────────────────────────────────────────┐
│         Tauri 桌面壳                      │
│  ┌────────────┐  ┌────────────────────┐   │
│  │  React 19  │  │  Rust 后端          │   │
│  │  (界面)    │  │  (命令路由、代理)    │   │
│  └─────┬──────┘  └─────────┬──────────┘   │
│        │                   │              │
└────────┼───────────────────┼──────────────┘
         │                   │
         ▼                   ▼
┌──────────────────────────────────────────┐
│     Python Sidecar (FastAPI + SSE)        │
│  ┌────────────────────────────────────┐   │
│  │        Agent 主循环                │   │
│  │  ├─ SSE 流式输出                   │   │
│  │  ├─ OpenAI 兼容工具调用            │   │
│  │  ├─ 权限分级（安全/确认）           │   │
│  │  ├─ 自验证管线                     │   │
│  │  └─ 记忆存储 (SQLite)              │   │
│  ├────────────────────────────────────┤   │
│  │      本地 LLM 引擎                 │   │
│  │  ├─ MLX（Apple Silicon 原生）      │   │
│  │  ├─ llama.cpp                      │   │
│  │  └─ 模型下载与管理                  │   │
│  └────────────────────────────────────┘   │
└──────────────────────────────────────────┘
```

## 🧩 技能系统

技能是 Latiao 的核心扩展机制。每个技能是一个 `sidecar/skills/` 下的 `SKILL.md` 文件，Agent 会根据任务上下文按需加载。

### 内置技能

| 技能 | 教给 Agent 什么 |
|------|----------------|
| `code-review` | 代码审查方法论与安全分析模式 |
| `git-workflow` | Git 提交规范与工作流最佳实践 |
| `python-fastapi` | Python FastAPI 惯用法与常见陷阱 |
| `typescript-react` | TypeScript React 开发模式与规范 |

### 创建自定义技能

```markdown
# sidecar/skills/my-skill.md
---
name: my-skill
description: 描述这个技能教给 Agent 什么
---

## 规则
1. Agent 应该遵守的第一条规则
2. 第二条规则

## 模式
- 场景 A → 执行 X
- 场景 B → 执行 Y

## 退出标准
- 判断任务完成的条件
```

放入 `sidecar/skills/` 目录，Latiao 自动识别加载。

## 📄 许可

MIT — 自由使用、修改和分发。
