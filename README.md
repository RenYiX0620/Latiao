

<p align="center">
  <img src="assets/screenshot.png" alt="Latiao Screenshot" width="700">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://tauri.app"><img src="https://img.shields.io/badge/Tauri-2.0-blue" alt="Tauri 2"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.11-brightgreen" alt="Python 3.11"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-blueviolet" alt="macOS + Windows">
  <a href="https://github.com/RenYiX0620/Latiao/releases"><img src="https://img.shields.io/github/v/release/RenYiX0620/Latiao" alt="Latest Release"></a>
  <a href="https://github.com/RenYiX0620/Latiao/releases"><img src="https://img.shields.io/github/downloads/RenYiX0620/Latiao/total" alt="Downloads"></a>
  <a href="https://github.com/RenYiX0620/Latiao/discussions"><img src="https://img.shields.io/badge/discussions-join%20us-9cf" alt="Discussions"></a>
</p>

---

# 🌶️ Latiao — Your Local AI Agent

> **An AI agent that lives on your computer — macOS & Windows. No cloud, no data leaks, your own models, your rules.**

Latiao is a desktop AI agent built with Tauri + React + Python FastAPI. It autonomously executes tasks on your computer — **read and write files, run shell commands, search the web, fetch market data, orchestrate sub-agents, schedule jobs** — while keeping your data completely private.

## What Can It Do?

| Capability | Description |
|------------|-------------|
| 📂 **File Operations** | Read, write, search, and organize files across your filesystem |
| 💻 **Command Execution** | Run shell commands, scripts, and development toolchains |
| 🌐 **Web Search** | Tavily (LLM-optimized) + Bing scraping — real-time news, docs, facts |
| 📈 **Market Data** | A-share/HK/fund quotes, capital flows, financial indicators (东方财富 data) |
| 🧠 **Multi-Model** | Local models (MLX / llama.cpp) or cloud APIs (OpenAI / DeepSeek / Anthropic) — switch anytime |
| 🤖 **Multi-Agent** | Delegate sub-tasks to specialists — explorer, code-reviewer, debugger, doc-generator, translator. Run in background and watch them work in the activity bar |
| 🧩 **Skills & Plugins** | SKILL.md knowledge packs + editable Python plugins (survive your edits across updates) |
| 💾 **Persistent Memory** | SQLite + TF-IDF semantic search — remembers across sessions |
| ✅ **Self-Verification** | Auto-validates its own work (re-reads files, runs linters, syntax checks, type-checks) |
| ⏰ **Scheduled Tasks** | Cron-style automation — recurring jobs, catch-up runs after restart |
| 🛡️ **5-Level Permissions** | read_only / confirm / auto_edit / plan / full — you decide how much autonomy it gets |
| 🌐 **Multilingual** | UI in English, 中文, 日本語, Русский |

## How It Works

```
You: "Fix all lint errors in src/"       Latiao:
                                            ├─ Reads every file in src/
                                            ├─ Runs ESLint on each
                                            ├─ Applies fixes
                                            ├─ Re-runs ESLint to verify
                                            ├─ Reports: "Fixed 12 errors across 5 files ✅"
                                            └── Heavy search work can be delegated to a background
                                                explorer sub-agent — you watch it live in the timeline
```

The agent loop streams its thinking and tool activity in real time via SSE — thinking rows, tool durations, and category-grouped actions ("Explore · 3 searches") render as a ZCode-style timeline. For sensitive operations (file writes, command execution), it asks for confirmation first. You're always in control.

## 📥 Install

| Platform | Requirement | Download |
|----------|-------------|----------|
| **macOS (Apple Silicon)** | M1–M4 | `Latiao_*.aarch64.dmg` |
| **Windows (x64)** | Win10+ | `Latiao_*_x64-setup.exe` / `.msi` |

Get the latest release from [GitHub Releases](https://github.com/RenYiX0620/Latiao/releases) — or the China mirror on [Gitee Releases](https://gitee.com/ryxo00/Latiao/releases) (much faster in mainland China).

**macOS**: double-click the `.dmg`, drag `Latiao.app` into `Applications`, launch. If Gatekeeper blocks the unsigned app: right-click → Open.

**Windows**: run the `setup.exe` installer. If SmartScreen warns: "More info" → "Run anyway".

**That's it. No Python, no Node.js, no setup required. Download and run.**

### 🔄 Auto-Update

Latiao updates itself in-app — silent background download (resumable, survives restarts), then install on your confirmation. Check manually anytime from **Settings → Check for Updates**.

### 📈 Financial Data (built-in, free tier)

Three-layer fallback chain — no API key for the first two:

1. **mx_query** — 东方财富 structured data (indices/sectors/stocks, capital flows, financials). Free tier: 150 calls/day.
2. **ak_finance** — AKShare open data (unlimited, no key). Index/stock quotes, spot data.
3. **tavily_search / bing_search** — live web search for news, overseas markets, sector rankings.

When the daily quota runs out, the agent automatically falls through to the next layer.

### 🧠 Local Model Compatibility Tips

- Works out of the box: MLX models (4bit/6bit/8bit), GGUF models (llama.cpp engine) — including LM Studio's folder-style layouts (`Model.gguf/Model.gguf`).
- New architectures may need a newer engine: very recent model types (e.g. `muse_glimmer`) aren't in the bundled `mlx-lm` yet — prefer mainstream models (Qwen, Ornith, GLM) or run those in LM Studio via the external-engine bridge.

## 🤖 Sub-Agents

Complex tasks get split. The main agent can delegate to specialist sub-agents — each with its own tool whitelist and identity:

| Agent | Tools | Best at |
|-------|-------|---------|
| **Explorer** | files + read-only shell + web search | Codebase recon, research, fact-finding |
| **Code Reviewer** | read-only files | Security & quality review |
| **Debugger** | files + whitelisted shell | Log analysis, bug hunting |
| **Doc Generator** | files + write | README/API docs/changelogs |
| **Translator** | files + write | i18n and localization |

Run them in the background (`background: true`) and watch steps, terminal commands, and file operations stream into the activity bar in real time — results land back in your chat when done.

## 🧩 Skills & Plugins

**Skills** are `SKILL.md` knowledge packs the agent loads on demand:

| Skill | What it teaches Latiao |
|-------|----------------------|
| `multi-search` | Web search strategies across engines |
| `mx-data` | Financial data querying patterns (东方财富) |
| `runcmd-patterns` | Safe shell command patterns |

Create your own by dropping a `SKILL.md` into `sidecar/skills/`.

**Plugins** are editable Python tools in `sidecar/plugins/` — read_file, run_cmd, tavily_search, bing_search, mx_query and more. Every plugin is a single `.py` file exposing `NAME`, `DEFINITION`, `execute()`. The loader preserves your modifications across app updates (hash-tracked seed manifest).

## 🚀 Development

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
npm run deploy
```

Hardened one-command build: cleans caches → provisions portable Python → verifies every resource file → bundles. Output:

- `Latiao.app` → `src-tauri/target/release/bundle/macos/`
- `Latiao_*.dmg` → `src-tauri/target/release/bundle/dmg/`

### Tests

```bash
cd sidecar && python -m pytest tests/ -q
```

## 🏗️ Architecture

```
┌──────────────────────────────────────────┐
│         Tauri Desktop Shell               │
│  ┌────────────┐  ┌────────────────────┐   │
│  │  React 19  │  │  Rust Backend      │   │
│  │  (UI)      │  │  (Commands, Proxy) │   │
│  └─────┬──────┘  └─────────┬──────────┘   │
└────────┼───────────────────┼──────────────┘
         ▼                   ▼
┌──────────────────────────────────────────┐
│     Python Sidecar (FastAPI + SSE)        │
│  ┌────────────────────────────────────┐   │
│  │        Agent Loop                  │   │
│  │  ├─ SSE streaming + watchdog       │   │
│  │  ├─ Tool calling (native + prompt) │   │
│  │  ├─ 5-level permission system      │   │
│  │  ├─ Sub-agent orchestration        │   │
│  │  ├─ Self-verification pipeline     │   │
│  │  └─ Memory store (SQLite)          │   │
│  ├────────────────────────────────────┤   │
│  │      Local LLM Engine              │   │
│  │  ├─ MLX (Apple Silicon native)     │   │
│  │  ├─ llama.cpp                      │   │
│  │  └─ Auto-reload & crash recovery   │   │
│  └────────────────────────────────────┘   │
└──────────────────────────────────────────┘
```

## 🔄 Updating

**v0.3.3 and later (fully automatic)**: Latiao checks for updates on launch, or use **Settings → Check for Updates**. When a new version is found → confirm → download (with progress) → install + auto-restart.

**v0.3.1 and earlier (manual upgrade needed once)**: older builds carry an old signing public key, and the matching private key password was lost — no valid update packages can be signed for them, so in-app online upgrade is unavailable. Please download and install the latest version manually from [Releases](https://github.com/RenYiX0620/Latiao/releases/latest) (Windows: run `*-setup.exe`; macOS: open the DMG and drag into Applications). From then on updates are fully automatic. Installing only replaces the app binaries: models (`~/Models`, `~/.cache/huggingface`), chat history, and settings are preserved.

## 📄 License

MIT — free to use, modify, and distribute.
