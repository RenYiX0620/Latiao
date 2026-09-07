> **中文** | [English](CONTRIBUTING.md)



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
cd sidecar && python -m pytest tests/ -q   # 后端测试
cd .. && npm run build                      # 前端 TS 编译
cd src-tauri && cargo check                 # Rust 编译
```

## 感谢

每一份贡献——代码、Bug 报告、文档或建议——都让辣条更好。
