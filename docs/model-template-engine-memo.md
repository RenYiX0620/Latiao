# 备忘录：模型 chat template 对照 & 引擎版本现状

日期：2026-09-23 ｜ 适用：辣条（Latiao）本机（macOS arm64，64GB）
结论一句话：**作者附带的 `chat_template.jinja` 对当前主力模型没有增益，维持内置模板**；
**引擎已于 2026-09-23 升级到上游最新 b11118（升级前落后 82 个 build），应用自身不感知引擎版本**。

---

## 一、那三个文件是什么

模型文件夹（`~/.lmstudio/models/…/Hermes3.6-35B-A3B-Uncensored-Genesis-Final-GGUF/`）里的：

| 文件 | 作用 | 能否换模型用 |
|---|---|---|
| `chat_template.jinja` (16.3KB) | 把 messages + 工具清单 + 思考开关序列化成模型训练时见过的 token 流；引擎按它拼输入、也按它解析工具调用 | **不能跨族**：绑死 Qwen 的 `<|im_start|>`/`<|im_end|>` 与训练时序列化方式。挂错模板不报错，只静默降级（工具不调用 / 思考泄漏 / 多轮格式乱） |
| `System_Prompt.txt` (6.3KB) | 作者推荐的总系统提示（通用助手版，含"先在心里按顺序做"脚手架） | 文本可搬，但脚手架是为推理型模型调的，小模型易变成"我将分几步做"的空转 |
| `System_Prompt_Agent.txt` (1.1KB) | 工具调用版提示（教 `<tools></tools>` 约定） | 只有目标模型的模板用同一约定时才有意义 |

**辣条默认都不用这三个**：它用自己的身份/工具提示词，模板取 **GGUF 内置**的那份
（`--jinja` 默认开；09-20 之后不再用 `--chat-template` 覆盖，避免把工具区/思考区一起换掉）。

补充事实：该 GGUF 内置模板（7.7KB）与随包 `.jinja`（16.3KB）**不是同一份**——后者多了
`tool_call_format` 开关、`auto_disable_thinking_with_tools`（默认 false）、更细的 `<tool_call>` 处理
（12 处 vs 4 处）。

## 二、A/B 实测（2026-09-23）

方法：**在 1236 端口另起一个同参数引擎**，只多挂 `--chat-template-file 作者那份.jinja`；
正在用的引擎全程不动。两边用**完全相同的请求体**（最小工具集 + 要求两次 read_file 的同一提示），
交错跑 3 轮 A/B。脚本：[`scripts/ab_chat_template.py`](../scripts/ab_chat_template.py)（可复用）。

| 指标 | A 内置模板（现状） | B 作者 `.jinja` |
|---|---|---|
| 模型调用轮次 | 2 / 2 / 2 | 2 / 2 / 2 |
| 原生 `tool_calls` | 2 / 2 / 2 全原生 | 2 / 2 / 2 全原生 |
| 文本式方言泄漏 | 无 | 无 |
| 第 1 轮思考 | 107–108 字 | 55–69 字 |
| **第 2 轮（工具结果轮）思考** | **0 字** | **150–184 字** |
| 第 2 轮耗时 | 0.7–1.0 s | 2.0–2.3 s |
| 回答准确性 | 3/3 正确 | 3/3 正确 |

**读法**：

- 工具调用行为**打平**（都是原生 JSON、零方言泄漏、两轮收口）→ 之前遇到的"工具调用方言乱"
  **不是这份内置模板造成的**，更可能来自别的模型（GLM/Bonsai 那类）或"关思考 / 超长上下文"路径。
- B 的代价是**跨轮保留思考**（引擎启动日志亦自述 `chat template supports preserving reasoning,
  it is enabled by default (may use more tokens)`）→ 工具结果轮多 150–184 字推理、耗时翻倍。
- 若将来要用 B 的其他特性，这个行为可关：`--no-reasoning-preserve`。

**边界（别过度外推）**：本测试用最小工具集 + 短提示；辣条真实请求带约 30 个工具与很长的身份提示，
属另一量级。要有定论需在真实请求下再比一轮（把模板挂进 `config.json` 的 `custom_engine.args`，
按 `match` 绑模型，重载后跑真实任务对比）。

## 三、引擎版本现状

| 项 | 值 |
|---|---|
| 本地引擎 | `version: 0.4.1-dev (build 11118, commit e6ab7c1a4)`（2026-09-23 升级前为 b11036） |
| 位置 | `Latiao.app/Contents/Resources/sidecar/llama-upstream/llama-server` |
| 上游最新 tag | **b11118**（`git ls-remote --tags https://github.com/ggml-org/llama.cpp`，v 系列最新为 v0.4.1） |
| 落后 | 0（已升到最新；升级前落后 82 个 build） |
| master 头 | `e6ab7c1a41054a888ada952eab4c886444c2f5ad` |
| 应用是否感知版本 | **不感知**：侧车无任何版本比较逻辑，`/v1/local-llm/fix` 只是环境修复，不是引擎升级 |

拿版本的两条命令：

```bash
# 本地
/Applications/Latiao.app/Contents/Resources/sidecar/llama-upstream/llama-server --version
# 上游最新（这台机器 curl/WebFetch 走不通——MITM CA 导致证书校验失败；git 可以）
git ls-remote --tags https://github.com/ggml-org/llama.cpp \
  | grep -oE "refs/tags/b[0-9]+$" | grep -oE "[0-9]+" | sort -n | tail -1
```

升级路径与风险：

```bash
bash scripts/fetch-upstream-llama.sh          # 自动挑"带 macOS arm64 包"的最新 release
```

- 该脚本用 `curl` 调 GitHub API + 下资产；**本机 curl 正常工作**（2026-09-23 实测 `curl https://api.github.com` → HTTP 200）。
  注意别给它加 `--cacert ~/.local-ai-os/ca-bundle.pem`：那份自签 bundle 反而会导致校验失败（HTTP 000），
  系统信任库本来就够用。（此前记的"curl 不通"是错的，害我误判过一次升级可行性。）
- 2026-09-23 已用该脚本升到 **b11118**（`version: 0.4.1-dev (build 11118, commit e6ab7c1a4)`）：
  参数兼容性实测通过（`-c 128000 --parallel 2` → `total_slots=2`、每槽 `n_ctx=64000`），原生工具调用正常。
  备份：`~/llama-upstream.bak-b11036`（引擎）、`~/Latiao.app.backup-0923-1330`（应用）。
- 引擎升级会引入**启动参数兼容风险**——历史上已经踩过两次：`-ngl -1` 被新版拒绝、
  `-fa` 从无值开关变带值（都会表现为"exited early / HTTP timeout"）。所以升级必须走
  "本地构建 → 本机预览 → 实测加载与工具调用 → 再推送"的流程，别直接换正式包。
- 升级后要复核的三项：`--parallel` / `-c` 与每槽窗口（`/props` 的 `total_slots` 与
  `default_generation_settings.n_ctx`）、`--cache-type-k/v` 的值形态、工具调用是否仍走原生。

## 四、换模型 / 换模板时的检查清单（复用）

1. 起 B 引擎时**换端口**，别动正在用的；跑完立刻 `kill $(lsof -ti tcp:1236)`（两份权重同时驻留很吃内存）。
2. 请求体两边完全一致（系统提示 + 工具 schema + 提示词），交错多轮抵消漂移。
3. 只看四个指标：轮次、原生 `tool_calls`、`markup_leak`、最终回答。
4. 模板/提示词的收益要**大于**它的代价（思考量、轮次、延迟）才改默认。
5. 别把模型的 `System_Prompt*.txt` 直接塞进辣条：它有自己的身份/工具提示词，换提示词等于引入新变量。

## 五、怎么判断 Metal（或其他后端）到底有没有更新

两条路，一条看上游提交、一条看本地二进制，互为交叉验证。

**A. 上游：按文件路径筛提交**（最直接，回答"有没有更新"）

```bash
# 最近动过 Metal 后端的提交（日期 + 摘要）
curl -s "https://api.github.com/repos/ggml-org/llama.cpp/commits?path=ggml/src/ggml-metal&per_page=12" \
  | python3 -c "import json,sys; [print(c['commit']['author']['date'][:16], c['sha'][:8], c['commit']['message'].splitlines()[0][:100]) for c in json.load(sys.stdin)]"

# 指定升级区间里到底改了哪些文件（含 backend 目录）
curl -s "https://api.github.com/repos/ggml-org/llama.cpp/compare/<旧commit>...<新tag>" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print([f['filename'] for f in d.get('files',[]) if 'metal' in f['filename']][:20])"
```

判读：提交日期晚于本地引擎的构建日期 → 这次升级**包含** Metal 变更；否则 Metal 没动。

**B. 本地：比对 Metal 库与内嵌 shader 字符串**（不需要网络，也不必看上游）

```bash
OLD=~/llama-upstream.bak-b11036 ; NEW=<现在>/sidecar/llama-upstream
for d in "$OLD" "$NEW"; do
  f=$(ls "$d"/libggml-metal*.dylib | head -1)
  printf "%s\n  库哈希 %s\n  shader 字符串 %s 字节 / 哈希 %s\n" "$d" \
    "$(shasum -a 256 "$f" | cut -c1-16)" \
    "$(strings -a "$f" | wc -c | tr -d ' ')" \
    "$(strings -a "$f" | shasum -a 256 | cut -c1-16)"
done
```

判读：**只看库哈希会被"重编译噪声"误导**（同一份源码重编也可能字节不同）；看 `libggml-metal` 的**字符串总量与哈希**更可信——
shader 源码是以字符串形式编进这个库的，字符串集合变了、体积也变了，说明 kernel 真的改了。

**2026-09-23 b11036 → b11118 的实测结论**：字符串 1,862,603 → 1,882,102 字节、哈希不同 → **Metal 确实更新**。
区间内（Sep 19–22）动 Metal 的提交：`metal : add MoE and SSM_CONV fusion optimizations`(09-19)、
`metal : fix FA support checks`(09-19)、`support qwen4exp hc ops`(09-19)、
`fix deprecation warnings from macOS 27 SDK`(09-21)、`simplify fusion pattern op list declaration`(09-21)、
`fix mask bounds in flash attention block pre-pass`(09-21)、`gate mul_mm_id src1 rescale behind ggml_prec`(09-22)。
其中 **MoE fusion 优化**对本机主力模型（qwen35moe / A3B）是潜在提速项。

### 升级的实测速度差（2026-09-23，同一台机器、两个引擎并存对照）

| 项目 | 旧 b11036 | 新 b11118 | 差 |
|---|---|---|---|
| 生成（decode，300 token） | 56.65 tok/s | 59.05 tok/s | **+4%** |
| 冷预填充（约 5.2k token） | 1212 / 1267 tok/s | 1329 / 1331 tok/s | **+5~9%** |

结论：**真实但温和**的提速（不是数量级）。若主观感觉"快很多"，另一部分来自对比基准——
升级当天的前半程机器上同时跑着两个引擎、两个定时任务和多次压测，Metal 被争抢，
那时的"慢"不是引擎基线。测速要点：**预填充必须每次换填充文本**，同一提示重复跑会命中
前缀缓存（实测缓存命中下能报出 36k tok/s 的假数字）。
