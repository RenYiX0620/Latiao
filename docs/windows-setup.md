# Latiao Windows 环境准备与构建指南

## 1. 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 64-bit 或 Windows 11 |
| 内存 | 至少 16GB（推荐 32GB） |
| 磁盘 | 至少 20GB 可用空间 |
| 网络 | 能访问 github.com 和 huggingface.co |

## 2. 安装依赖

### 2.1 前置工具

用 **Windows Terminal (管理员)** 按顺序装：

```powershell
# 安装 Chocolatey（包管理器）
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

# 安装 Rust
choco install rust-msvc -y

# 安装 Node.js 22 LTS
choco install nodejs-lts -y

# 安装 Python 3.12（不要用 3.14，llama-cpp-python 还不支持）
choco install python --version=3.12.9 -y

# 安装 Git
choco install git -y

# 安装 jq（build-win.sh 需要）
choco install jq -y

# 重启终端让 choco 装的工具生效
```

或者不用 Chocolatey，手动下载安装：
- [Rust](https://rustup.rs/) — 运行 rustup-init.exe
- [Node.js](https://nodejs.org/) — 下载 22.x LTS MSI
- [Python 3.12](https://www.python.org/downloads/) — 下载安装时勾选 **Add to PATH**
- [Git for Windows](https://git-scm.com/download/win)
- [jq for Windows](https://jqlang.github.io/jq/download/) — 下载 jq.exe 放到 `C:\Windows\System32\`

### 2.2 验证安装

```powershell
rustc --version
node --version
npm --version
python --version
git --version
jq --version
```

全部应有版本输出。

## 3. 配置 Rust 工具链

Tauri 需要 MSVC 工具链，Rust 默认使用 MSVC：

```powershell
# 确认默认工具链是 msvc
rustup default stable-msvc
rustup target list --installed | findstr msvc

# 如果报错缺 linker，安装 Visual Studio Build Tools
# 下载 https://visualstudio.microsoft.com/visual-cpp-build-tools/
# 安装时勾选：
#   - 使用 C++ 的桌面开发
#   -  Windows 10/11 SDK
# 或者直接装 Visual Studio Community 2022（更省事）：
choco install visualstudio2022community -y
```

## 4. 克隆并构建

### 4.1 克隆项目

```powershell
cd C:\
git clone https://github.com/RenYiX0620/Latiao.git
cd Latiao
```

### 4.2 安装前端依赖

```powershell
npm install
```

### 4.3 安装 sidecar Python 依赖

```powershell
cd sidecar
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt   # 如果有的话
pip install fastapi uvicorn httpx certifi
deactivate
cd ..
```

> 注意：Windows 上的 sidecar 依赖不在 `llama-cpp-python`，因为 Windows 用原生 `llama-server.exe`。
> 只需要装：`fastapi`, `uvicorn`, `httpx`, `certifi`。

### 4.4 构建 Windows 发布包

```powershell
# 需要管理员 PowerShell（build-win.sh 会下载 llama-server.exe 和打包）
bash scripts/build-win.sh
```

如果 `bash` 不可用（Git Bash 没装），手动分步执行：

**Step 1：下载 llama-server.exe**
```powershell
$llamaTag = (curl -s https://api.github.com/repos/ggml-org/llama.cpp/releases/latest | jq -r '.tag_name')
curl -L "https://github.com/ggml-org/llama.cpp/releases/download/${llamaTag}/llama-${llamaTag}-bin-win-cpu-x64.zip" -o $env:TEMP\llama.zip
Expand-Archive -Path $env:TEMP\llama.zip -DestinationPath .\sidecar\ -Force
# 解压后把 llama-server.exe 移到 sidecar\ 根目录
```

**Step 2：PyInstaller 打包 sidecar**
```powershell
cd sidecar
.\venv\Scripts\activate
pip install pyinstaller
pyinstaller latiao.spec
copy .\dist\sidecar.exe .\   # 拷贝到 sidecar\ 根目录（tauri 打包时会包含）
deactivate
cd ..
```

**Step 3：Tauri 构建 MSI**
```powershell
npm run tauri build
```

MSI 生成在：
```
src-tauri\target\release\bundle\msi\Latiao_<version>_x64.msi
```

双击该 MSI 安装，或在终端运行：
```powershell
msiexec /i src-tauri\target\release\bundle\msi\Latiao_*.msi
```

## 5. 首次运行验证

### 5.1 验证 sidecar 启动

安装后启动 Latiao，检查 sidecar 进程：

```powershell
# 查看 sidecar.exe 是否运行
Get-Process sidecar -ErrorAction SilentlyContinue

# 查看 HTTP API 是否响应
curl -s http://127.0.0.1:8000/v1/heartbeat
```

### 5.2 验证模型加载

在 Latiao 的 Settings 中添加一个 GGUF 模型路径，加载后确认：
- CPU 推理正常（无 GPU 的机器会慢，但能跑）
- 对话能收到回复

### 5.3 验证 auto-update

检查 Settings 中是否有自动更新弹窗（如果当前版本 < GitHub 最新版本）。

## 6. 常见问题

### Q: `link.exe` 找不到 / LNK 错误

缺 MSVC 工具链。装 Visual Studio Build Tools 或 Visual Studio Community，并确保 `rustup default stable-msvc`。

### Q: PyInstaller 打包后 sidecar.exe 闪退

试试在 PowerShell 中直接运行看什么报错：
```powershell
.\sidecar\sidecar.exe
```
常见原因：缺 `fastapi` / `uvicorn` / `certifi` 等 hidden import。
解决方法：在 `latiao.spec` 的 `hiddenimports` 列表中补充。

### Q: `tauri build` 报 `can't find crate` 之类的 Rust 错误

```powershell
# 更新 Rust
rustup update
# 清理重 build
cd src-tauri
cargo clean
cd ..
npm run tauri build
```

### Q: MSI 安装后找不到 sidecar.exe

检查 `build-win.sh` 的 step 3（copy .\dist\sidecar.exe .\）是否执行了。
`tauri build` 打包的是 `sidecar\` 目录下的文件，所以 `sidecar.exe` 必须在 `sidecar\` 根目录。

### Q: --mx-query 模式运行报错

```powershell
.\sidecar\sidecar.exe --mx-query "贵州茅台股价"
```

确认 skill 文件路径正确：`sidecar.exe` 同级目录下应有 `skills\mx-data\mx_data.py`。
如果路径不对，检查 `latiao.spec` 的 `datas` 配置是否包含了 `skills/` 目录。

## 7. 目录结构（构建后）

```
Latiao/
├── sidecar/
│   ├── main.py            # 源码（仅标记用，已打入 exe）
│   ├── sidecar.exe         # PyInstaller 打包产物
│   ├── llama-server.exe    # 原生 llama.cpp
│   ├── latiao.spec         # PyInstaller 配置
│   ├── agents/             # agent 配置（动态读取）
│   ├── skills/             # 技能（动态读取）
│   └── plugins/            # 插件（动态读取）
├── src/                    # React 前端
├── src-tauri/
│   └── target/release/bundle/msi/Latiao_*.msi  # 最终安装包
└── scripts/
    └── build-win.sh        # Windows 构建脚本
```
