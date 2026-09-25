# Latiao Windows 版（本仓库 = Windows 适配版）

本分支/仓库为 **Latiao 的 Windows 适配版本**，基于原版（macOS 优先）代码，针对 **Windows 10/11 x64** 做了完整适配与增强。

> 原版仓库：[RenYiX0620/Latiao](https://github.com/RenYiX0620/Latiao)（macOS 优先）

## 🆕 本版新增功能

### 免 API Key 工具（Windows 版独有）

| 工具 | 功能 | 数据源 | 需要 Key |
| --- | --- | --- | --- |
| `bing_search` | 网页实时搜索 | cn.bing.com（国内直连） | ❌ 无需 |
| `ak_finance` | A股/港股/指数/基金行情 | 腾讯行情 + AKShare/东方财富 | ❌ 无需 |

原版自带的 `tavily_search`（网页搜索）与 `mx_query`（金融）需要 API Key，Windows 版新增以上两个免 Key 工具作为替代，开箱即用。

## 🔧 Windows 适配修复（相对原版）

1. **GBK 编码崩溃**：`identity.py` / `local_llm.py` / `main.py` 中 `read_text()` 未指定 `encoding="utf-8"`，中文 Windows 默认 GBK 编码导致 sidecar 启动即崩 → 已修复
2. **DeepSeek V4 思考模式 400**：DeepSeek V4 思考模式下多轮请求必须回传 `reasoning_content`，否则返回 400 → 对 DeepSeek 模型请求注入 `thinking: {"type": "disabled"}`
3. **DeepSeek 400 "Tool names must be unique"**：`delegate_task` 被重复注册导致工具名重复 → 工具列表三层去重
4. **DeepSeek 400 孤儿 tool_call**：历史消息中 assistant 带 tool_calls 但缺少对应 tool 结果时 400 → 发送前自动补齐
5. **Windows 密钥存储**：原版 Windows 的 `get_secret` 永远返回 "Not found"（云模型配置重启即丢）→ 改为文件存储（`%APPDATA%\latiao\secrets\`）
6. **mx_query 崩溃**：`skills/mx-data` 目录名含连字符无法被 Python/PyInstaller 识别 → 改名为 `skills/mx_data` + spec 添加 hiddenimports
7. **前端依赖缺失**：补装 `@tauri-apps/plugin-opener`（代码引用但 package.json 缺失）
8. **Windows 依赖清单**：新增 `sidecar/requirements-win.txt`（去除 macOS 专属的 mlx 系列）

## 🏗️ Windows 构建

环境要求：Git for Windows / Node.js 20+ / Python 3.11 / Rust MSVC / VS Build Tools（C++ 桌面开发）

```bash
# 依赖
npm install
cd sidecar && pip install -r requirements-win.txt "pyinstaller==6.*" && cd ..

# 构建（Git Bash 中执行）
bash scripts/build-win.sh

# MSI 产物
# src-tauri/target/x86_64-pc-windows-msvc/release/bundle/msi/Latiao_0.1.4_x64_en-US.msi
```

> 注：国内网络环境（GitHub 被墙）时，构建脚本已适配 gh-proxy 镜像；crates 走 rsproxy 镜像（见 `~/.cargo/config.toml`）。

## 📌 本版说明

- 本仓库以 Windows 为主要目标平台；macOS 功能（MLX 引擎等）保持代码兼容但未在本机验证
- 构建产物（sidecar.exe / llama-server.exe / *.dll / MSI）不入库，请按上述步骤自行构建
- 更新日期：2026-08-13
