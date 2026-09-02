# 📣 社区发文草稿（掘金 / V2EX 通用版）

> 发布前请：①替换「XX」为你的真实数据 ②配上 2-3 张实际截图 ③根据平台调语气
> 掘金：技术向，突出架构；V2EX：精简，突出"我做了个东西"的分享感

---

# 我给 Mac 写了个本地 AI Agent：不上云、不联网也能干活，开源了

## 为什么做这个

市面上的 AI 桌面助手基本两条路：要么纯云端（数据全上去、按 token 付费），要么套壳网页（功能受限）。但我想要的是第三种：**一个真正跑在本机的 Agent，能读写我的文件、执行命令、查数据，而所有推理都可以走我自己的本地模型**。

于是用几个月业余时间写了辣条（Latiao）：Tauri 2 + React 19 + Python FastAPI 三层架构，Rust 宿主管进程和代理，Python sidecar 跑 Agent 循环。

## 它能干什么

- **自主执行任务**：发一句"把 src/ 里所有 lint 错误修了"，它自己读文件 → 跑 ESLint → 改代码 → 重跑验证 → 汇报，全程流式可见
- **本地模型推理**：MLX（Apple Silicon 原生）/ llama.cpp 双引擎，Qwen/Ornith/GLM 的 MLX 和 GGUF 都能跑，不花钱不联网
- **云端自由切**：配置 OpenAI/DeepSeek/GLM 的 API 也行，本地/云端按任务自动路由
- **金融数据**：内置东方财富结构化查询 + AKShare 免费兜底 + 联网搜索三层降级（写 A 股盯盘的人狂喜）
- **多智能体**：复杂任务自动拆给探索者/代码审查员/调试专家等子智能体，活动栏实时看它们干活
- **定时任务**：cron 表达式驱动，模型自己创建，"每 10 分钟盯一次大盘"这种需求一句话搞定
- **持久记忆**：SQLite + TF-IDF，跨会话记得你的偏好
- **五级权限**：从只读到完全访问，敏感操作先确认

## 几个值得一提的技术细节

1. **双引擎健康检查**：mlx_lm.server 在模型加载完成前就返回 200（假就绪），踩过"刚启动就发请求→空响应→误判引擎死亡→杀进程重载"的死循环后，就绪探测改成了真实 chat 探测
2. **推理模型的工具调用**：Qwen3.8 这类推理模型会把 token 全花在思考上，content 为空——就绪判定和健康检查都不能按"有文字"来判断，得认 reasoning 字段
3. **多种工具调用格式**：不同模型输出 ```tool 栅栏、裸 JSON、<tool_call> XML 三种格式，解析器全兼容
4. **引擎恢复链**：空响应→标记存疑→健康探测→杀进程→自动重载→排队请求自动恢复，整套自愈不需要用户干预

## 下载

- macOS（M1+）：[Releases](https://github.com/RenYiX0620/Latiao/releases) 下载 DMG
- Windows（x64）：[Releases](https://github.com/RenYiX0620/Latiao/releases) 下载 setup.exe
- 国内镜像：[Gitee Releases](https://gitee.com/ryxo00/Latiao/releases)

安装包自带完整 Python 运行时，零依赖。

## 开源

MIT 协议，[GitHub 仓库在这](https://github.com/RenYiX0620/Latiao)。技能是 Markdown 知识包、插件是单文件 Python，欢迎写自己的技能包分享，也欢迎 PR。

---

## V2EX 精简版（发 #create 或 #python 节点）

> 标题：[分享] 写了个本地 AI Agent，Tauri+Python，本地模型推理+工具调用+定时任务，开源了

不想让数据上云，又想让 AI 在本机干活（读写文件、跑命令、盯行情），试了几个月堆出来这个：https://github.com/RenYiX0620/Latiao

- Tauri 2 + React + Python FastAPI，Rust 侧管进程和代理
- 本地模型 MLX（M 系列）/llama.cpp 双引擎，云端 API 可选可切
- Agent 循环：SSE 流式、工具调用（原生+prompt 式）、子智能体、自验证、SQLite 记忆
- 金融数据三层兜底（东财结构化 / AKShare / 联网搜索）
- macOS M1+ 和 Windows x64 都有安装包，自带运行时零依赖

MIT 开源，欢迎 star / 提 issue / 写插件。

---

## 知乎版开头（回答"有哪些开源本地 AI Agent"类问题）

推荐一个我持续开发的：辣条（Latiao），GitHub 开源（MIT）。

它和一般"本地 Agent"不同的点：不是 CLI 工具，是完整的桌面应用（Tauri 壳 + Python Agent 循环），macOS/Windows 双平台安装包，下载即用。模型走本机 MLX/llama.cpp 推理（也有云端可选），工具能真正读写你的文件、执行命令、查 A 股数据、跑定时任务——不是玩具演示，是可以当生产力用的那种。

架构上比较有意思的：Rust 宿主做进程管理和请求代理，Python sidecar 跑 Agent 循环（SSE 流式、五级权限、子智能体编排、自验证管线），前端 React 19。对本地 AI Agent 感兴趣的可以看下源码，Agent 循环的设计（引擎自愈、多格式工具解析、意图路由）值得一看。
