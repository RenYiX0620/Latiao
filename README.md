# 🌶️ Latiao — Your Local AI Agent Desktop App

> **An AI Agent that runs locally on your machine. No cloud, no data leaks, your own models.**
>
> ⚠️ **Currently macOS only.** See Windows notes below.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tauri](https://img.shields.io/badge/Tauri-2.0-blue)](https://tauri.app)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green)](https://python.org)

Latiao is a desktop AI Agent app built with Tauri + React + Python FastAPI. It can autonomously execute tasks on your computer — read files, run commands, search code, manage projects. All data and code stay local, fully private.

<p align="center">
  <img src="assets/screenshot.png" alt="Latiao Screenshot" width="700">
</p>

## ✨ Features

- 🏠 **Fully Local** — All agent logic runs on-device, no cloud needed
- 🧠 **Multi-Model** — Local models (llama.cpp / MLX) + Cloud APIs (OpenAI / DeepSeek / Anthropic)
- 🔧 **Tool System** — Agent can read files, execute commands, search code, open apps
- 🎭 **Multi-Agent** — Built-in Code Reviewer, Debugger, Doc Generator, Translator
- 🧩 **Skills System** — Extensible SKILL.md plugins, load domain knowledge on demand
- 💾 **Memory System** — SQLite + TF-IDF semantic search, cross-session knowledge retention
- ✅ **Self-Verification** — File re-read, ESLint, Python syntax, TypeScript type checking
- ⏰ **Cron Jobs** — Scheduled automation tasks
- 🌐 **Multilingual UI** — English / 中文 / 日本語 / Русский

## 📥 Download

```bash
# Install Git first: https://git-scm.com
git clone https://github.com/RenYiX0620/Latiao.git
cd Latiao
```

## 🚀 Quick Start

### Requirements

- **OS**: macOS (Windows / Linux not yet supported)
- Python 3.10+
- Node.js 20+
- Rust (for Tauri)

### Run (macOS)

```bash
# 1. Enter project
cd Latiao

# 2. Install frontend dependencies
npm install

# 3. Install Python dependencies
cd sidecar
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd ..

# 4. Start dev mode
npm run tauri dev
```

### Production Build

```bash
npm run deploy
# Output: ./Latiao.app (macOS)
```

## 🪟 Windows Users

Latiao currently supports **macOS only** because:

- Uses macOS-specific APIs (e.g. system Keychain for secrets)
- Path and permission models differ from Windows
- Tauri build target not configured for Windows

**Workarounds:**

1. Use WSL2 (Windows Subsystem for Linux)
2. Use a VM (VMware / VirtualBox with macOS)
3. Wait for community contributions (PRs welcome!)

Interested in porting to Windows? Check [CONTRIBUTING](#) or open an Issue.

## 🧩 Skills System

The Skills system is Latiao's core extension mechanism. Each skill is a `SKILL.md` file under `sidecar/skills/`.

### Built-in Skills

| Skill | Description |
|-------|-------------|
| `code-review` | Code review & security analysis |
| `git-workflow` | Git workflow conventions |
| `python-fastapi` | Python FastAPI best practices |
| `typescript-react` | TypeScript React dev standards |

### Creating Custom Skills

```markdown
# sidecar/skills/my-skill.md
---
name: my-skill
description: What this skill does
---

## Rules
1. First rule
2. Second rule

## Exit Criteria
- Conditions that must be met
```

## 🏗️ Architecture

```
┌─────────────────────────────────────┐
│  Tauri Desktop App (Rust + React)   │
│  ┌───────────┐  ┌──────────────────┐ │
│  │  Frontend  │  │  Tauri Commands  │ │
│  │ (React 19) │  │   (Rust)         │ │
│  └─────┬─────┘  └────────┬─────────┘ │
│        │                  │           │
└────────┼──────────────────┼───────────┘
         │                  │
         ▼                  ▼
┌─────────────────────────────────────┐
│  Python Sidecar (FastAPI)           │
│  ┌──────────────────────────────┐   │
│  │      Agent Loop              │   │
│  │  ├─ _agent_loop_stream       │   │
│  │  ├─ Tool execution           │   │
│  │  ├─ Self-verification        │   │
│  │  └─ Memory (SQLite)          │   │
│  ├──────────────────────────────┤   │
│  │  Local LLM Engine            │   │
│  │  ├─ llama.cpp / MLX          │   │
│  │  └─ Model download & mgmt    │   │
│  └──────────────────────────────┘   │
└─────────────────────────────────────┘
```

## 📚 Docs

- [Agent System](#) — Agent loop, tool execution, verification loop
- [Memory System](#) — SQLite + TF-IDF semantic search + auto-distillation
- [Skills System](#) — Progressive retrieval + auto-generation
- [Harness Engineering](#) — Agent reliability (instruction / tool / env / state / feedback layers)

## 🤝 Contributing

Contributions welcome!

- 🐛 Submit bug reports
- 💡 Propose features
- 🧩 Contribute skills
- 📝 Improve docs

## 📄 License

MIT License — free to use, modify, and distribute.

---

**Your models, your agent, your data.** 🌶️

---

---

# 🌶️ 辣条 Latiao — 你的本地 AI Agent 桌面应用

> **跑在你本机上的 AI Agent。不需要联网，不偷你的代码，用你自己的模型。**
>
> ⚠️ **当前仅支持 macOS**。Windows 用户参考上方英文说明。

Latiao（辣条）是一个桌面 AI Agent 应用，基于 Tauri + React + Python FastAPI 构建。它能在你的电脑上自主执行任务——读取文件、执行命令、搜索代码、管理项目，所有数据和代码都在本地，完全隐私。

## ✨ 核心特性

- 🏠 **完全本地** — 所有 Agent 逻辑跑在本机，不需要云服务
- 🧠 **多模型支持** — 本地模型（llama.cpp / MLX）+ 云端 API（OpenAI / DeepSeek / Anthropic）
- 🔧 **工具系统** — Agent 可以读取文件、执行命令、搜索代码、打开应用
- 🎭 **多 Agent** — 内置代码审查员、调试专家、文档生成器、翻译助手
- 🧩 **技能系统** — 可扩展的 SKILL.md 插件，按需加载领域知识
- 💾 **记忆系统** — SQLite + TF-IDF 语义搜索，跨会话持久化知识
- ✅ **自验证** — 文件回读、ESLint、Python 语法、TypeScript 类型检查
- ⏰ **定时任务** — Cron 风格的定时自动化
- 🌐 **多语言界面** — English / 中文 / 日本語 / Русский

## 📥 下载到本地

```bash
git clone https://github.com/RenYiX0620/Latiao.git
cd Latiao
```

## 🚀 快速开始

### 环境要求

- **操作系统**：macOS（暂不支持 Windows / Linux）
- Python 3.10+
- Node.js 20+
- Rust (for Tauri)

### macOS 安装运行

```bash
cd Latiao
npm install

cd sidecar
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd ..

npm run tauri dev
```

### 生产构建

```bash
npm run deploy
# 输出: ./Latiao.app (macOS)
```

## 🧩 技能系统

每个 skill 是一个 SKILL.md 文件，放在 `sidecar/skills/` 目录。

### 内置技能

| 技能 | 描述 |
|------|------|
| `code-review` | 代码审查与安全分析 |
| `git-workflow` | Git 工作流规范 |
| `python-fastapi` | Python FastAPI 最佳实践 |
| `typescript-react` | TypeScript React 开发规范 |

### 创建自定义技能

```markdown
# sidecar/skills/my-skill.md
---
name: my-skill
description: 描述你的技能
---

## 规则
1. 第一条规则
2. 第二条规则

## 退出标准
- 必须满足的条件
```

## 🏗️ 架构

```
┌─────────────────────────────────────┐
│  Tauri 桌面应用 (Rust + React)       │
│  ┌───────────┐  ┌──────────────────┐ │
│  │   前端 UI   │  │  Tauri Commands  │ │
│  │ (React 19) │  │   (Rust)         │ │
│  └─────┬─────┘  └────────┬─────────┘ │
│        │                  │           │
└────────┼──────────────────┼───────────┘
         │                  │
         ▼                  ▼
┌─────────────────────────────────────┐
│  Python Sidecar (FastAPI)           │
│  ┌──────────────────────────────┐   │
│  │      Agent Loop              │   │
│  │  ├─ _agent_loop_stream       │   │
│  │  ├─ 工具执行                  │   │
│  │  ├─ 自验证 (_auto_verify)     │   │
│  │  └─ 记忆系统 (SQLite)         │   │
│  ├──────────────────────────────┤   │
│  │  Local LLM Engine            │   │
│  │  ├─ llama.cpp / MLX          │   │
│  │  └─ 模型下载与管理             │   │
│  └──────────────────────────────┘   │
└─────────────────────────────────────┘
```

## 📚 文档

- [Agent 系统设计](#) — Agent loop、工具执行、验证闭环
- [记忆系统设计](#) — SQLite + TF-IDF 语义搜索 + 自动提炼
- [技能系统设计](#) — 渐进式检索 + 自动生成
- [Harness 工程](#) — Agent 可靠性保障（指令层 + 工具层 + 环境层 + 状态层 + 反馈层）

## 🤝 贡献

- 🐛 提交 Bug 报告
- 💡 提出功能建议
- 🧩 贡献 Skill
- 📝 改进文档

## 📄 许可

MIT License — 自由使用、修改、分发。
