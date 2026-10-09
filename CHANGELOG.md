# 更新日志（CHANGELOG）

**约定（2026-09-25 起）**：每次发版都补一份「用户视角」的发布说明，并且

1. 写进 GitHub Release 的 body（`gh release edit <tag> --notes-file ...`）——用户点开 release 就能看到；
2. 同步追加到本文件（一键：`python3 scripts/changelog_add.py <版本> <说明文件>`，已存在则拒绝重复写入）。

说明写"用户会看到什么"，不写实现细节；安全与稳定性问题照写，但不点名可利用细节。

**Windows 下载指引（写进每版说明）**：要自动更新请下 `.exe`（`x64-setup.exe`）——**`.msi` 装的应用收不到内置更新**（Tauri 限制），只适合批量部署。

---

## v0.3.48 — 真正的 Word / PDF / Excel，引擎不再自己等死

### 📄 让它写文档：现在直接产出真正的 Word / PDF / Excel

- 说"以 **Word** 格式分析今天大盘" → 直接得到 `.docx`：真样式（标题居中、正文首行缩进两字符、1.5 倍行距）、真表格（表头底色、数字右对齐、跨页重复表头）、页脚页码
- 说"要 **PDF**" → 直接出 PDF（A4、页脚页码）；中文用**随包字体**，不依赖你系统装了什么字体，也便于发给别人
- 说"要 **Excel**" → 出 `.xlsx`（Markdown 表格自动分工作表）
- 版式按**正文语言**自动切换（中文 / 日文 / 西文各有对应规范）
- 不再出现"模型给你一个 .py 生成脚本、要你自己跑"的情况

### 🛠 稳定性

- 本地引擎意外退出时不再"自己把自己等死"：自动重载期间不会被误判为卡死，界面显示"正在自动重载…"（此前会报"⏱ 流式响应超时"）
- 引擎崩溃会留下日志（`~/.local-ai-os/engine.log`）与退出码；加载成功也记录耗时，便于定位问题
- 写入二进制文件（如 Word）不再让整轮对话崩掉
- 命令被拒绝/超时后，结果里不再被错误标记为"✅ 成功"

### 🔒 安全

- 补齐命令通道封印：`permissions.json` / `agents.json` / `skills.json` 不可被命令或写入工具改写（此前只封了一半，能借此持久关掉确认弹窗）
- 危险重定向（`2>&1`、`2>/dev/null`）此前会被**静默执行错**（把 `2` 当参数、输出丢进 /dev/null），现在明确拒绝并说明原因
- Windows：修复"双开互杀后端""重启后端第一次失败""同名进程误杀"等问题

### 📦 打包

- 依赖（python-docx / reportlab / pillow）与随包中文字体（Noto Sans SC，OFL 许可）已进入安装包 —— 热更的部分从此不再被更新冲掉

---

## v0.3.50 — Windows 两个修复（鉴权读取 + 窗口透明）

## 辣条 v0.3.50 — Windows 两个修复

### 🪟 Windows
- **启动即"鉴权失效"**：Windows 上 sidecar 读不到启动令牌（`select()` 在 Windows 只支持 socket，对 stdin 直接报 WinError 10038）→ 界面报"sidecar 不可用"、请求 401。现改为**线程 + 队列**带超时读取，Windows 可正常取到令牌。
- **窗口透明引发的显示异常**：Windows 上关闭窗口透明与 hudWindow 效果（此前会出现窗口/内容显示异常）。
- 更新通道保持开启：**装上本版后仍能自动更新**（Windows 修复版把 `updater.active` 关掉了，那会让应用再也收不到更新，本版没有采纳）。

### 🔒 安全
- 保持**失败关闭**：令牌未初始化时一律拒绝（含 `/health`）。Windows 的令牌读取问题在读取处修，不靠放宽鉴权兜底。

---

## v0.3.51 — macOS 观感恢复（Windows 修复只对 Windows 生效）

## 辣条 v0.3.51 — macOS 观感恢复

### 🍎 macOS
- **恢复窗口透明与系统磨砂（vibrancy）**：上一版误把 Windows 的显示修复当成两平台共用，macOS 的磨砂被一起关掉了；本版恢复原样。
- 上一版（0.3.50）已标记为预发布，**不会**通过自动更新下发。

### 🪟 Windows（保持修复）
- 启动鉴权修复（令牌读取）、窗口显示不再异常、更新通道保持开启。
- 从本版起 Windows **只出 `.exe` 安装器**——要自动更新请下 `.exe`（`x64-setup.exe`）；`.msi` 装的应用收不到内置更新（Tauri 限制），只适合批量部署。

---

## v0.3.52 — Windows 浅色模式侧栏可读性修复

## 辣条 v0.3.52 — Windows 浅色模式：左侧栏看不清修复

### 🪟 Windows
- **浅色模式下左侧栏按钮/名字看不清**：关闭窗口透明后，侧栏失去了 macOS 那样的磨砂底，浅底+浅字几乎不可读。现在 Windows 上侧栏自带实底并保证文字对比度（macOS 不受影响，仍走窗口磨砂）。
- 说明：这是**止血版**——若还有个别控件（图标、悬停态）对比度不对，请指出具体位置，下一版精修。

---

## v0.3.53 — 行情不再说错时间，按钮和 AI 共用一套能力

### 📈 查行情：时间不再张冠李戴

- 问「美股现在怎么样」时，回答里的时间点以**系统换算为准**：北京 / 美东双时钟、开盘中还是已收盘、已开盘多久——不再出现「还没开盘」其实已经开了、把上午说成下午、把收盘时间算错 3 小时这类硬错
- 美股「现在 / 盘中」直接给**带时间戳的实时点位**，不再靠网上新闻猜
- 修掉一个会骗人的数据源：某美股接口返回的是几个月前的冻结数字（偏差 10–20%），现在会直接屏蔽并提示改用可靠来源
- 「美股」二字也能查了——此前问「美股怎么样」匹配不到，会掉进网页搜索拿到过期数字

### 🔔 说到做到

- 模型说「收盘叫你 / 到点提醒你」时，必须**当场建好定时任务**；建不了会明说「我没法定时提醒你」，不再开空头支票

### 🎛 快捷调用：按钮和 AI 同一份实现

- 工具页新增「快捷调用」：选工具、填参数、一键执行，结果进同一台账
- 这些按钮**和 AI 走同一套工具代码**——改一次两边同时生效，不会再出现「按钮一个样、AI 一个样」
- 高危工具（如写文件）需点「确认并执行」，和 AI 调用同一套权限；确认凭证由**服务端签发**（一次性），本地脚本无法伪造「我已确认」

### 📦 其他

- Windows：下载请用 `.exe`（`x64-setup.exe`）才能自动更新；`.msi` 装的收不到内置更新

---

## v0.3.54 — 接通道、断得更少、账更清楚

### 🔌 通道接入：飞书、微信都能找辣条干活

- **飞书**：填一次应用凭据，就能在飞书里直接给辣条发消息（记账、查数据、写文档都行），
  支持长连接，**不需要公网地址**；只认文字消息，图片/文件会提示先转成文字
- **微信**：走腾讯官方的 OpenClaw 插件 + 一次扫码授权，同样能把消息转给辣条
- 通道里的对话按会话存进辣条的会话列表——你在界面上能看到通道里聊过什么，
  多轮上下文自动延续（和界面里聊天是同一个大脑、同一套记忆）

### 📈 行情：少一类"看起来对其实是旧的"数字

- 「美股现在怎么样」直接给带时间戳的实时点位（此前只给过网页新闻里的旧数字）
- 会骗人的冻结数据源已屏蔽（某接口返回的是几个月前的快照，偏差可达 10–20%）
- 行情查询失败时，提示改为"该接口持续拒连、重试无效，请改用 mx_query"——
  不再建议重试一次这种无效动作

### 🧠 记忆：不再被自己过去的报错带偏

- 「某工具当时额度用完了」这类**临时性**的失败记录，24 小时后不再注入（并自动清理）；
  结构性的经验（比如"某工具不支持某类查询"）继续保留
- 起因：一条六天前的"额度已用完"旧记录，让模型绕开了本来能用的工具

### 🔁 循环更会刹车

- **预算守卫**：单轮输入超过阈值时先收口让模型用已有数据作答，仍超就停手交付
- **卡住就交回**：同一工具以同一错误连续失败达三次，停止重试并把"试过什么、卡在哪"交给你
- **旧工具输出回收**：很早的、体积大的工具结果折叠成摘要，长对话不再被旧数据挤满
- 语气在"强制收口作答"那一轮也会带上（此前表格类长回答容易掉回中性语气）

### ✅ 说到做到 & 不编造

- 给出链接前必须确认它真的指向所称内容（实测过：一篇文章末尾的项目地址指向了无关项目）
- 数字仍须带来源与时点；查不到的字段写"未查询到该日数据"，不填占位符

### 🗑 移除：快捷调用面板

- 工具页的「快捷调用」面板（选工具、手填 JSON、手动执行）按你的判断移除——
  它更适合排障用途，对日常使用没帮助
- 对应的后端直调端点一并删除（减少一个绕过权限档的入口）
- ⚠️ 若你正在用 v0.3.53 里的这个面板，本次更新后它会消失

### 🔧 其他

- 状态栏的运行指标修正：此前偶发出现"首 token 平均 26 秒""278001 tok/s"这类
  明显失真的数字（统计口径错误），现在要么给真实值，要么不显示
- GitHub 仓库发现功能支持从配置里读取 token（提高搜索配额）；未配置时行为不变
- Windows：下载请用 `.exe`（`x64-setup.exe`）才能自动更新；`.msi` 装的收不到内置更新

---

## v0.3.55 — 修一处"卡住判定"的误判

### 🔁 连续失败判定更准

- 一个回合里**同时发出多个不同查询**、而它们恰好都失败时（例如密钥缺失、网络不通），
  此前会被误判成"在原地空转"而提前收口——现在这种情况按"覆盖查询"正常继续；
  只有**换着说法反复撞同一堵墙**（同样的错误连续三轮）才会停下来交回给你
- 附带效果：这一轮的工具调用统计不再被跳过（此前误判发生时，当轮的检索计数会漏记，
  影响后续的"检索预算"判断）

### 🔧 其他

- 内部边界整理（工具路由模块的请求解析统一到单一实现），无行为变化
- Windows：下载请用 `.exe`（`x64-setup.exe`）才能自动更新；`.msi` 装的收不到内置更新

---

## v0.3.56 — Loop 工程收官：闸门、成本可见、三套循环合一

# Latiao v0.3.56 — Loop engineering: gates, cost visibility, one single loop

This release finishes the loop-engineering work: every safety gate now lives on **one** loop, and
what a turn costs is finally visible.

## New

- **Cost visibility.** The context panel now shows two extra rows: this turn's input tokens against
  the budget, generation, **resample/retry counts with reasons**, and the "knowledge refinement"
  calls (the invisible extra model call after each tool) — plus a per-source breakdown
  (main loop / sub-agents / refinement). At 80% of the budget the app now tells you explicitly
  instead of silently wrapping up, and scheduled jobs are finally accounted for (they used to be
  invisible).
- **Per-turn metrics persisted.** One row per turn (input/output tokens, retries, TTFT, duration,
  source breakdown, end reason) so you can look up "which turn was slowest/most expensive",
  kept for 180 days by default.
- **Task-level verifier.** Before delivering, the app mechanically checks the turn's own output:
  a file it claimed to write that is missing or half-written (truncated JSON), tests still failing
  while the answer claims success, artifacts named in the answer that do not exist. It then asks
  the model to finish or to say plainly where it is stuck. **Deleting or emptying test files to
  "pass" is blocked** — you no longer get a fake "done".
- **Loop discipline in the system prompt.** On repeated failure the model reports the blocker and
  hands back to you instead of grinding to the step limit.

## Improvements

- **Scheduled jobs now run on the same loop as chat.** Task verification, budget guard, stall and
  same-error escalation, context compaction and tool-result recycling used to be bypassed by cron
  entirely. Timeout / cancel / exception are now **recorded and visible** (previously a job could
  fail silently and you would just see nothing). Jobs also accept an explicit tool access mode
  (e.g. read-only).
- **Tool-result recycling by category.** File reads and searches fold earlier (saving context);
  market/finance results that contain numbers are kept longer (so digits are not lost and then
  misquoted). Restored history is recycled too, and the fold marker names the tool.
- **More reliable compaction.** No more cutting mid-line (numbers and filenames are no longer
  split in half), with an explicit note of what was dropped plus "re-run the tool if you need a
  specific number — do not quote from memory".
- **The non-streaming endpoint and frontend history replay** now truncate on line boundaries too,
  and the endpoint runs on the single loop.
- **Input-token self-check.** Both calibers of the input token count are stored, and a smoke alarm
  fires in the log if the engine's semantics change.

## Fixes (found by audit)

- **Anti-cheat false positives:** `rm -rf build/ && pytest tests/`, moving a test file to a backup
  location, or writing an empty `tests/__init__.py` were misjudged as tampering and handed the turn
  back to you. Fixed — while also catching forms that used to slip through (e.g. `sudo rm`).
- **Failure-judge false positives:** reading source that merely contains "not found"-style wording,
  or a search that happens to hit those words, was counted as a tool failure and could eventually
  hand the turn back. It now only treats an error-shaped first line as a failure.
- **Onboarding mis-capture:** during first-run onboarding a short scheduled-task name (e.g. "日报")
  could be recorded as your name. A non-interactive gate now prevents this.
- **Three state leaks in the test suite** (which routed later tests to a stopped stub engine / deleted
  a module attribute), fixed.
- One-step budget overshoot now warns before wrapping up; large PDF deliverables are no longer read
  into memory in full for validation.

## Notes

- Every gate in this batch was verified by "the old code must fail" (A/B) checks; sidecar 1238 tests
  and 100 frontend tests are green.
- Please trigger one scheduled job manually in the app to confirm real-model behavior (the log shows
  it running on the single loop).
- The three cost/source rows in the panel appear with this build.

---

## v0.3.57 — 引擎升到 b11284 + 轮次生命周期修复

# Latiao v0.3.57 — new inference engine + turn-lifecycle fixes

This release ships a newer local engine and fixes the two problems that could freeze or
falsely abort a turn.

## Engine

- **llama.cpp engine upgraded** (bundled engine build b11256 → **b11284**, via the same
  "fetch the newest upstream build at release time" path used for macOS and Windows).
  Smoke-tested on this machine with a small GGUF and with the 35B-A3B daily-driver model:
  loads and generates normally. The GGML runtime version is unchanged (0.25.3), so this is
  an application-level bump rather than a format change.

## Fixes

- **Long "thinking only" replies no longer get cut off at 180s.** Thinking output used to be
  held back by the language-drift gate and only released once the visible answer started,
  so a model that reasoned for several minutes produced *no bytes at all* — and the client's
  180-second inactivity watchdog aborted the turn even though the backend was still working.
  Thinking is now streamed immediately (the drift check still applies to the visible answer).
- **A turn that hangs can no longer lock the session.** If the engine stops producing data,
  the previous behaviour left the session marked "running" — you could not send a new message
  and even the stop button appeared to do nothing (the stop path is different from the
  watchdog path). Now: the engine-wait window is capped inside the watchdog window (170s,
  `LATIAO_ENGINE_WAIT_MAX`), and a session whose turn exceeds the stale threshold (180s,
  `LATIAO_TURN_STALE_SECS`) is force-released so new messages go through.
- **Per-step logs now record TTFT** (time to first token), so a slow turn can be told apart
  from a silent one without watching the screen.

## Also included (were committed after v0.3.56)

- **`mx_query` now checks the name before giving up.** When a query returns nothing, the tool
  looks the name up in Eastmoney's public search and tells the model either the nearby
  official board names (e.g. "超节点" → 算力概念 BK1134 / 液冷服务器 BK1138) or that the name
  does not exist — instead of the old wording that pushed the model into trying synonyms.
- **Fallback ladder in the system prompt** (all four UI languages): official board name →
  supply-chain stocks → web search, and "replace the name before switching tools".
- **`ak_finance` description** now states it covers industry boards only and has no fund-flow
  data, so the model stops trying it for concept boards or capital flows.

## Notes

- Backend-only changes: no new UI is required for any of these.
- If a turn ever stalls, the log line `[THIN][step N] 流结束 … 耗时=… TTFT=…` distinguishes
  "engine slow" (TTFT small, duration large) from "silent" (TTFT missing).

---

## v0.3.58 — v0.3.58 — engine bump to latest upstream

## Engine

- Bundled llama.cpp updated to the latest upstream build (b11382+, fetched at release build time). No configuration change needed — the new engine ships with the installer.

## Fixes

- **Custom-engine reload with bare model names** (found live in the occamy reload incident): auto-reload passes a bare model name without a path; the custom-engine branch of `start_model` failed silently ("model path not found", no error logged), cleared engine state and left the UI showing "engine not running". Bare names are now resolved through the same fuzzy scan the standard branch uses. Directory layouts also prefer the same-named inner main GGUF, so the mmproj projector can no longer be picked up as the main model by alphabetical order.
- **Tool-call dialect parsing** now extracts JSON with a `raw_decode` position scan instead of greedy/anchored regex: the ```json fence tolerates leading prose, `<tool_call>{json}` survives trailing brace junk, and the Qwen `<function=…>` mixed dialect no longer produces garbage argument keys. Array-wrapped call objects are accepted as well.

## Developer tooling

- `scripts/dual_judge.py`: grade one batch of answers with two LLM judges from different model families and report the agreement rate — a low rate means the rubric (not the tested variant) is the problem, so fix the ruler before trusting any number. Intended for one-variable-at-a-time A/Bs.

---

## v0.3.59 — v0.3.59 — usage statistics dashboard

## New

- **Usage Statistics page**: a dedicated view in the sidebar — five summary cards (all-time tokens with total turns, peak day, longest turn, current/longest day streaks), a GitHub-style token-activity heatmap (26 weeks, columns aligned to Monday; daily / weekly / cumulative modes) and a per-model daily token trend with 7/30-day ranges.
- **Usage history in the context meter**: the meter popover now offers a "Usage history" toggle — totals, local/cloud split, average local TTFT, per-day bars and a recent-turns table (all sessions or the current session only).

## Notes

- Usage rows are recorded per turn (since v0.3.56) and kept for 180 days; the heatmap fills in as history accumulates.
- The current-streak counter counts from yesterday when today has no usage yet, so the number does not reset at midnight before your first turn of the day.

---

## v0.3.60 — v0.3.60 — usage dashboard + narration leak fixes

## New

- **Usage Statistics page**: a dedicated view in the sidebar — five summary cards (all-time tokens with total turns, peak day, longest turn, current/longest day streaks), a GitHub-style token-activity heatmap (26 weeks, columns aligned to Monday; daily / weekly / cumulative modes) and a per-model daily token trend with 7/30-day ranges. The smaller usage-history panel inside the context meter popover remains as a quick view.

## Fixes

- **Duplicate round narrations no longer pile into the answer**: when a model repeats the same opening narration in every tool round (observed with a local MoE model), the duplicate rounds are now withdrawn from the delivered reply and only the first occurrence is kept. Chat bubbles are now sliced per round, and the non-streaming/cron paths apply the same semantics.
- **Omission markers are no longer written into model-facing history**: truncated narration markers were being imitated by the model (it began writing its own pseudo-summaries into real answers); duplicate-narration history entries are now empty, leaving nothing to imitate.

---

## v0.3.61 — 助手更懂你说过的话（记忆治理）+ 任务清单面板

## ⚡ 工作任务清单（新）

- 多步任务（改多个文件、调研+实现、批量处理）时，助手会在对话上方显示一份**任务清单**：当前做到哪一步、还剩几步，一眼可见（✓ 已完成 / ● 进行中 / ○ 待办）

## 🧠 记忆更准了

- **聊天内容不再被当成你的长期设定**。此前你在对话里说的话可能被记成"偏好"——比如复述一段文章、随口问的一句"我想买一手现在这个价格是底了吧"，之后每次相关对话都被重新带出来，影响助手的判断。这次清掉了 300 多条这类噪音（**只归档不删除**，随时可恢复）
- **更会记你真正说过的偏好**。此前只有极少数句式能被记住（比如恰好说成"以后……回复……"），"以后简短点"、"记住我用中文"这类话都会被漏掉——你可能觉得"说过的话它记不住"。现在这些表达都能识别；同时保留原有保护：**你说过一次的话只先记为候选，重复表达过的才会真正生效**，避免一句话永久改变助手行为
- **新增「记忆手册」**：`~/.local-ai-os/memory/handbook.md`，按分区展示助手记住的一切（偏好 / 纠正 / 技术知识 / 项目结构…），随时可翻阅、核对

## 🛠 稳定性

- **记忆注入的反馈不再空转**。此前"这条知识有没有帮上忙"需要手动点赞才记录（实际几乎没人点，机制形同虚设）；现在自动判断"注入的知识是否出现在回答里"，用于持续校准注入的精准度——长期会让该带的知识更常出现、不该带的越来越少
- 注入统计（设置页里的学习数量/平均置信度）口径修正为**只统计活跃记忆**，不再把已归档的算进去

---

## v0.3.62 — v0.3.62 — version reporting fix

## Fixes

- **The app now reports its own version correctly.** v0.3.61 was built with an internal version mismatch: the installed app believed it was 0.3.60. As a result the update check kept seeing a "newer" version forever — updating and restarting never cleared the update prompt, and the version shown in Settings stayed at the old number. This release compiles the correct version into the app; after this update the banner stops reappearing and Settings shows the right version.

---

## v0.3.63 — v0.3.63 — usage stats load fix + docked confirmation card

## Fixes

- **The Usage Statistics page now loads its data.** It used to fetch once, about a second after app start — while the sidecar backend was still booting — and never retried, so the page stayed empty on every launch even though the data was there. It now loads when you open the page, retries with backoff, and shows an explicit "failed to load / Retry" state instead of a silent empty view if the backend is unreachable.
- **The Agent management page had the same startup race** (fetched once at app start, no retry) and is fixed the same way.

## Improvements

- **The tool-confirmation card (Allow once / Always allow / Deny) now docks above the input bar.** Previously it lived inside the transcript and could scroll out of view; now it appears right where you are typing, with the transcript keeping the tool row as a plain record.

---

## v0.3.64 — v0.3.64 — stop key everywhere + usage page polish

## Fixes

- **The Stop button now stops immediately, at every stage.** Previously, pressing Stop during a long generation only took effect after the current step finished (one case ran 108 seconds past the press), and the tool the model had just requested still executed afterwards (a file was written 75 seconds after Stop). Cancellation is now honored mid-generation (~0.5s), before tool execution, while a tool command is running (the command's process tree is killed), while queued for the engine, and while waiting for an engine reload.

## Improvements

- **Usage Statistics: a Clear button.** Erases all recorded usage (sessions, chat history and memory are untouched; counting restarts with the next turn) after a confirmation dialog. The page computes its numbers fresh on every load, so clearing takes effect immediately.
- **Usage Statistics: working hover tooltips.** Hovering a heatmap cell now shows date / input / generated / turns, and hovering a trend point shows date · model · tokens (the previous tooltips never appeared inside the app's webview).

---

## v0.3.65 — v0.3.65 — usage dashboard visual pass

## Improvements

- **Usage Statistics: cleaner trend chart.** The same model no longer appears as two separate lines when older records stored its full file path and newer ones its short name — the trend now merges them into one series (fewer, cleaner lines with no duplicated legend entries). Lines are smoothly curved (Catmull-Rom) instead of jagged point-to-point segments.
- **Hover interaction on the trend chart.** Data dots no longer sit permanently on the lines; hovering a day lights up that day's points for every model and shows a tooltip listing each model's token count for that day.
- **Working hover tooltips on the heatmap** (date, input, generated, turns per cell) — the previous ones never appeared inside the app's webview.

## New

- **Usage Statistics: a Clear button** to erase all recorded usage after a confirmation dialog. Sessions, chat history and memory are untouched; counting restarts with the next turn.

---

## v0.3.66 — v0.3.66 — lag-free typing

## Fixes

- **Typing in the input box is no longer laggy.** The input value lived at the app's top level, so every keystroke — including every pinyin candidate update during composition — re-rendered the whole app including the entire chat history. With long chats, letters appeared one by one after the pinyin was already typed. The input is now uncontrolled: the IME writes straight to the DOM with zero re-renders while composing, and typing only re-renders the input box itself. Chinese IME protections (Enter no longer sends mid-composition), image paste, and voice-dictation injection all behave exactly as before.

---

## v0.3.67 — v0.3.67 — engine b11457 with K2 Horizon support

## Engine

- **llama.cpp updated to b11457** — this build includes the newly merged **K2 Horizon architecture support** (upstream PR #29535, merged 2026-10-06): dense and MoVA variants, full inference graph on existing GGML operators, plus the K2 chat template with a dedicated reasoning/tool-call parser. IFM/K2-Horizon-MoVA-36B-A4B GGUF and community variants can now be loaded.
- Note: K2 Horizon on macOS Metal has not been independently benchmarked yet — you may be among the first to run it outside CUDA. If a load or generation fails, please report the sidecar log.

## Fixes

- Carries the v0.3.63–v0.3.66 fixes for new installs: stop button now takes effect at every stage of a running turn, usage dashboard data loading and hover tooltips, model-name merge in the trend chart, the clear-stats button, and the lag-free typing input.

---

## v0.3.68 — v0.3.68 — diagnostic build (jank probe)

## Diagnostic

- **Built-in jank probe.** This version ships a lightweight runtime probe to gather hard evidence for the reported typing lag: long main-thread blocks (>50ms) and slow chat renders (with message counts) are aggregated and written to the sidecar log every ~30s. Update, use the app normally for a few minutes (especially typing in your longest session), and the log tells us exactly which of the suspected costs — stream flushes, markdown re-parsing, periodic state storms — actually dominates. No functional changes in this build.

---

## v0.3.69 — v0.3.69 — no more half-finished tasks after tool failures

## Fixes

- **Tasks no longer stop halfway after a tool failure.** When a data tool failed (e.g. the eastmoney endpoint refusing connections), the model would sometimes reply with only a short promise — "I'll look into these right away~" — and end its turn, so the task died mid-way. The agent now recognizes these promise fragments in the exact context they occur (previous tool failed, short reply, promise wording, no actual data) and pushes the model to either retry with a different tool or deliver a complete conclusion.
- Also carries the diagnostic jank probe from v0.3.68 for new installs.

---

## v0.3.70 — v0.3.70 — engine bump to b11487

## Engine

- **llama.cpp updated to b11486** (from b11474). This range is dominated by continued fixes on the newly merged architectures (including K2 Horizon) and general Metal/backend improvements — CI fetches the newest upstream build at package time.

---

## v0.3.71

## v0.3.71 — 任务不再停在半路

### 🛡 助手不再"说一句要做什么"就停下

以前会出现的场景：它说"让我拉一下实时数据给你看～"或"让我再深挖一下研报内容～"，然后就没了下文——任务半途而废，你得再问一次。

现在这一轮一定会以**结果**收尾：

- 它只说不做时，助手会**自动催它继续动手**（最多两次；如果这一轮还什么数据都没拿到，催三次）；
- 催了仍然只说不做 → **强制它基于已经拿到的数据直接给答案**（或明确告诉你还缺什么），不会再交给你一句"让我去查"；
- 没有拿到任何数据时，收尾指令会**明确要求它如实说明"没取到数据"**，并禁止编造数字——宁可说"这次没查到"，也不编一个看着像真的答案。

连带修掉的三个漏网形态（都是真实对话里抓到的）：结尾带表情装饰（"…给你看～💋"）、把"查/搜"换成"深挖/核对"这类同义说法、以及"数据够了但答不出来"的收尾阶段同样只说计划的情况。

### 🔍 启动时不再闪出一条看不懂的提示

之前每次启动都会一闪而过地弹一条消息（"正在检查更新…"或"更新失败：…"）——这是后台检查在启动瞬间撞上软件自身还没就绪导致的**误报**，实际什么事都没有。现在启动时的自动检查真正静默：只有"更新包已准备好、下次重启自动安装"这类**你确实需要知道**的消息才会提示。

### 🧾 顺带

- 新增"交付哨兵"：万一以后模型又发明新的"只说计划不动手"说法漏过判定，会先在日志里留痕，便于我主动发现，而不是等你再遇到一次。

---

**Windows 用户**：要自动更新请下载 `.exe`（`x64-setup.exe`）——**`.msi` 安装的应用收不到内置更新**（Tauri 限制），只适合批量部署。

---

## v0.3.72

## v0.3.72 — 命令写法更宽容一点

### 🧾 现在接受 `2>&1` 和 `2>/dev/null`

以前你在对话里会看到这样的连环失败：助手写的命令带 `2>&1` 或 `2>/dev/null`，被一句"⛔ 不支持 shell 操作符"退回，它换个写法又被退回。

- **现在这两种写法照做**：`2>&1` 把错误信息并进正常输出（助手能自己看到报错、自己纠正）；`2>/dev/null` 丢弃错误信息；管道两侧也能带（`head f 2>&1 | grep x`）。
- **仍然拒绝的**（会继续大声拒绝、不会静默跑错）：`&&`、`;`、`||`、`<`、多条管道，以及会写文件的 `2> 文件`——这些在"不经过 shell"的执行方式下没有安全的等价实现，硬跑会把符号当成参数传给程序。
- 工具说明与拒绝文案也同步改成了和新行为一致的说法（以前它们还把 `2>&1` 列在"不支持"里）。

---

**Windows 用户**：要自动更新请下载 `.exe`（`x64-setup.exe`）——**`.msi` 安装的应用收不到内置更新**（Tauri 限制），只适合批量部署。

---

## v0.3.73

## v0.3.73 — 空回复修掉了，查数据更少走弯路

### 🩹 修掉一个会让你看到"空白回复"的问题

有一种情况会导致你问了问题、助手却回给你一条**空消息**（有思考、没正文）：当回答的开头是英文、触发"语言检查"后重试、重试的开头又是英文时，程序在收尾路径上漏了一步——已经生成好的内容没有发给你。现在修好了：**任何情况下都不会再出现空白回复**，重试的内容会完整送达。顺带把判据放宽（正常中文回答以英文术语开头，如 `SOP = Standard Operating Procedure（标准作业程序）…`，不再被误判成"回答跑成英文了"而整段丢弃）。

### 🧭 查行情时少走弯路

- 金融工具的使用说明补上了**失败换路指引**：某些网络环境下东财数据源会直接拒连，以前助手会在这个工具上原地重试、甚至卡住只说一句"我这就去查"；现在它明确知道：**失败一次就改用备用数据源**，不重试、不发空承诺。
- 文件类工具补上了"别绕路"说明：读文件/列目录/找文件/写文件都直接用专用工具，不再用命令行 `cat/ls/find/echo>` 绕一圈（更快、不用逐条确认）。

### 📊 数据文件不再被"截断"

长会话里，早先读过的**数据文件**（JSON / 表格 / CSV）以前会按"它是哪个工具读的"来决定保留多少——模型自己读回来的数据表也可能被压到只剩 250 字，模型于是说"数据被截断了/工具坏了"。现在**按内容形状判断**：只要是数据形态，就按最高档保留（留 900 字 + 更晚才回收）。用你机器上三份真实导出文件（最大 147k 字符）验证过识别正确。

### 🔍 新增两道"自查"机制（后台运行，不打扰你）

- **数字溯源哨兵**（观察期）：助手回答里的百分比、小数、点位/金额，如果在这一轮的工具结果里找不到出处，会记一条日志。观察几天误伤率之后，再决定要不要升级成"打回重答"。实测真实语料：93 个数字里 5 个无出处，多为引用第三方宣称（如"某卖家用 AI 优化广告 CPA 降低 65%"）——正是该标注来源的形态。
- **回归重放进 CI**：把你实际遇到过的每一类"说一句就停"的原句（7 条）钉成回归基线，每次发版自动重放；改坏任何一条，构建当场失败、不会发到你手上。

---

**Windows 用户**：要自动更新请下载 `.exe`（`x64-setup.exe`）——**`.msi` 安装的应用收不到内置更新**（Tauri 限制），只适合批量部署。

---

## v0.3.74

## v0.3.74 — 回复不再重复，更新不再看"墙外"脸色

### 🧹 修掉"回复两遍"

有几类本地模型会把"内心戏"先写进正文、用一个 `</think>` 标记收尾，然后才写正式回复——以前这两版都会显示出来，看起来就是同一段话回了两遍。现在：

- 那条收尾标记会被识别，**前半段自动撤回**（你会看到它一闪后被清掉，或者干脆不出现）；
- 只有正式回复留在对话里，标记本身也不会显示；
- 顺带修了一个藏了很久的同类问题：清洗结果在主流程里被丢弃，导致"标记剔除"这项工作做了但没生效——这次一并根治。

### 🔄 更新流程不再依赖 GitHub 是否连得上

你上次更新 v0.3.73 失败，原因不在包的完整性（包其实早就下好了），而是两条链路里有一条必须"直连 GitHub 拿清单"，而这台机器访问 GitHub 时好时坏。现在：

- **本地优先**：更新清单改从本机的辣条服务读取（本来就是它负责下载更新包的），GitHub 只作为兜底；
- **已下载的包不再被误判**：以前如果"拿清单"这一步超时，会把已经下好的包也判成失败；现在这种情况会保留已完成的包，你点一下更新就能直接装上。

也就是说：**从这一版起，自动更新只依赖"能连上本机的辣条服务"，不再依赖"能不能连上 GitHub"。**

---

**Windows 用户**：要自动更新请下载 `.exe`（`x64-setup.exe`）——**`.msi` 安装的应用收不到内置更新**（Tauri 限制），只适合批量部署。

---

## 更早版本

以下标题来自各自的发布提交（完整产物与发布时间见 GitHub Releases）：

- **v0.3.47** — 对话框显示的模型就是真正回答的模型；中文不再等翻译
- **v0.3.46** — 语言安全网恢复工作
- **v0.3.45** — 点"停止"不再把应用带崩
- **v0.3.44** — 给助手改名终于能生效
- **v0.3.43** — 记忆系统审计、会话改为后端权威、凭据收敛
- **v0.3.42** — 多会话并行、定时任务收口、安全加固
- **v0.3.41** — 界面语言统一
- **v0.3.40** — 语音语言过滤、音调滑杆、情绪、自动朗读
- **v0.3.39** — 恢复记忆笔记、不再伪造空结果、清理死代码
- **v0.3.38** — 100 个本地音色可选、人格卡片刷新
- **v0.3.37** — 朗读回复（先框架：系统语音，本地语音服务后续接入）
