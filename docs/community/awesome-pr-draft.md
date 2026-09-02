# 📋 Awesome 列表 PR 草稿（待用户审阅后提交）

> 提交步骤：①Fork 目标列表 ②在合适分类下加条目 ③PR 描述用下方文案
> 推荐目标列表（按匹配度排序）：
> - awesome-macOS（https://github.com/serhii-londar/open-source-mac-os-apps）→ Development 类
> - awesome-llm-apps（https://github.com/Shubhamsaboo/awesome-llm-apps）
> - awesome-ai-agents（https://github.com/e2b-dev/awesome-ai-agents）
> - awesome-local-llm（https://github.com/rlancemartin/awesome-local-llms）
> - awesome-tauri（https://github.com/tauri-apps/awesome-tauri）→ Apps 类

## 条目文案（英文）

```
[Latiao](https://github.com/RenYiX0620/Latiao) — A privacy-first desktop AI agent for
macOS (Apple Silicon) & Windows. Tauri 2 + React 19 + Python sidecar. Runs local models
via MLX/llama.cpp (or cloud APIs), with autonomous task execution: file operations, shell
commands, web search, financial data (eastmoney), multi-agent orchestration, cron jobs,
and a 5-level permission system. Ships as a zero-dependency installer.
```

## 条目文案（中文列表用）

```
[辣条 Latiao](https://github.com/RenYiX0620/Latiao) — 隐私优先的本地 AI 桌面智能体
（macOS Apple Silicon / Windows）。Tauri 2 + React 19 + Python sidecar，本地模型推理
（MLX / llama.cpp）或云端 API，自主执行任务：文件读写、Shell 命令、联网搜索、金融行情
（东方财富）、多智能体编排、定时任务，五级权限控制。安装包自带运行时，零依赖。
```

## PR 描述模板

```
## Add Latiao to <分类名>

[Latiao](https://github.com/RenYiX0620/Latiao) is an open-source (MIT) desktop AI agent
that runs local models (MLX on Apple Silicon, llama.cpp on Windows) entirely on-device,
with a tool-calling agent loop (files/shell/web/financial data), sub-agent orchestration,
scheduled tasks, and a 5-level permission system. Ships installers for macOS (M1+) and
Windows x64 with zero runtime dependencies.

Fits this list's <分类名> section because it is <理由，按列表要求改写>。
```

## 注意事项

- 每个 PR 只加一个列表，条目描述按该列表的现有格式微调
- 先看列表的 CONTRIBUTING 是否要求"先开 issue"
- 被拒/无回应属正常，一次投 3-5 个列表，成功率最高的是 awesome-macOS
