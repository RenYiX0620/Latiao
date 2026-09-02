# 🤝 Contributing to Latiao / 参与贡献

感谢你对辣条的兴趣！欢迎任何形式的贡献——不限于代码。

## 🐛 报告 Bug

用 [Issue 模板](https://github.com/RenYiX0620/Latiao/issues/new/choose)提报，附上：
- 平台（macOS / Windows）和版本号（设置页可见）
- 复现步骤和现象
- 相关日志（`~/.local-ai-os/sidecar.log` 中关键报错行）

## 💡 提建议

功能想法请优先发到 [Discussions → Ideas](https://github.com/RenYiX0620/Latiao/discussions/categories/ideas)，讨论成型后再转 Issue。

## 🔧 代码贡献

```bash
git clone https://github.com/RenYiX0620/Latiao.git
cd Latiao && npm install
cd sidecar && pip install -r requirements.txt && cd ..
npm run tauri dev   # 开发模式热重载
```

**提交前自检：**

```bash
cd sidecar && python -m pytest tests/ -q   # 后端测试（238 个）
cd .. && npm run build                      # 前端 TS 编译
cd src-tauri && cargo check                 # Rust 编译
```

**约定：**

- 后端改动请配套测试（`sidecar/tests/`）
- 前端文案走 i18n（`src/i18n/translations.ts`，四语言都要填）
- 插件是单文件 Python（`sidecar/plugins/`），暴露 `NAME`、`DEFINITION`、`execute()`
- 提交信息用中文或英文均可，说清楚"为什么"而不只是"改了什么"

## 🧩 贡献技能 / 插件

- **技能（SKILL.md）**：放进 `sidecar/skills/<name>/`，教 Agent 新领域的知识
- **插件（.py）**：放进 `sidecar/plugins/`，给 Agent 新工具

写好后开 Discussion → Show and tell 展示，优秀的会收进默认发行版。

## 📖 文档

README / WINDOWS.md / 翻译修正直接 PR。文档和代码同等重要。
