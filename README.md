# 🌶️ Latiao — Your Local AI Agent Desktop App

> **跑在你本机上的中文 AI Agent。不需要联网，不偷你的代码，用你自己的模型。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tauri](https://img.shields.io/badge/Tauri-2.0-blue)](https://tauri.app)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green)](https://python.org)

Latiao（辣条）是一个桌面 AI Agent 应用，基于 Tauri + React + Python FastAPI 构建。它能在你的电脑上自主执行任务——读取文件、执行命令、搜索代码、管理项目，所有数据和代码都在本地，完全隐私。

<p align="center">
  <img src="assets/screenshot.png" alt="Latiao Screenshot" width="700">
</p>

## ✨ 核心特性

- 🏠 **完全本地** — 所有 Agent 逻辑跑在本机，不需要云服务
- 🧠 **多模型支持** — 本地模型（llama.cpp / MLX）+ 云端 API（OpenAI / DeepSeek / Anthropic）
- 🔧 **工具系统** — Agent 可以读取文件、执行命令、搜索代码、打开应用
- 🎭 **多 Agent** — 内置代码审查员、调试专家、文档生成器、翻译助手
- 🧩 **技能系统** — 可扩展的 SKILL.md 插件，按需加载领域知识
- 💾 **记忆系统** — SQLite + TF-IDF 语义搜索，跨会话持久化知识
- ✅ **自验证** — 文件回读、ESLint、Python 语法、TypeScript 类型检查
- ⏰ **定时任务** — Cron 风格的定时自动化
- 🌐 **多语言** — 界面支持中文 / English / 日本語

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Node.js 20+
- Rust (for Tauri)

### 安装运行

```bash
# 1. 克隆仓库
git clone https://github.com/yourusername/latiao.git
cd latiao

# 2. 安装前端依赖
npm install

# 3. 安装 Python 依赖
cd sidecar
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cd ..

# 4. 启动开发模式
npm run tauri dev
```

### 生产构建

```bash
npm run deploy
# 输出: ./Latiao.app (macOS)
```

## 🧩 技能系统

Latiao 的技能系统是它的核心扩展机制。每个 skill 是一个 SKILL.md 文件，放在 `sidecar/skills/` 目录。

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

欢迎贡献！你可以：

- 🐛 提交 Bug 报告
- 💡 提出功能建议
- 🧩 贡献 Skill
- 📝 改进文档

## 📄 许可

MIT License — 自由使用、修改、分发。

---

**用你的模型，跑你的 Agent，数据在你手里。** 🌶️
