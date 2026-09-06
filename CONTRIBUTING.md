> **English** | [中文](CONTRIBUTING.zh.md)



# 🤝 Contributing to Latiao

Thank you for your interest! Contributions of any kind are welcome — not just code.

## 🐛 Reporting Bugs

Use the [Issue templates](https://github.com/RenYiX0620/Latiao/issues/new/choose). Please include:
- Platform (macOS / Windows) and version number (visible in Settings)
- Reproduction steps and the observed behavior
- Relevant log lines from `~/.local-ai-os/sidecar.log`

## 💡 Feature Ideas

Please post ideas to [Discussions → Ideas](https://github.com/RenYiX0620/Latiao/discussions/categories/ideas) first, then convert to an Issue once the discussion matures.

## 🔧 Code Contributions

```bash
git clone https://github.com/RenYiX0620/Latiao.git
cd Latiao && npm install
cd sidecar && pip install -r requirements.txt && cd ..
npm run tauri dev   # dev mode with hot reload
```

**Self-checks before committing:**

```bash
cd sidecar && python -m pytest tests/ -q   # backend tests
cd .. && npm run build                      # frontend TS compile
cd src-tauri && cargo check                 # Rust compile
```

## Thank you

Every contribution — code, bug report, docs, or feedback — makes Latiao better.
