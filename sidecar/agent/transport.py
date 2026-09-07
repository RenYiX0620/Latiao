"""本地引擎传输层（host）：串行锁、引擎忙注册、健康验证、流封装与恢复。"""
import asyncio
import os
import time
import logging
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx

import local_llm

logger = logging.getLogger("latiao-sidecar")


def mark_llm_suspect() -> None:
    """标记引擎疑似损坏（流取消/空响应后调用），下一次本地请求先做健康验证。"""
    global _llm_suspect_since
    _llm_suspect_since = _llm_suspect_since or time.monotonic()


def clear_llm_suspect() -> None:
    global _llm_suspect_since
    _llm_suspect_since = None


def _safe_cwd() -> str:
    """获取当前工作目录。部署时 app 被 rm -rf 重建,运行中 sidecar 的 CWD
    指向已删除目录,os.getcwd() 会抛 FileNotFoundError → 回退 home。"""
    try:
        return os.getcwd()
    except OSError:
        return str(Path.home())


# llama_cpp.server 是单模型实例，多个流式生成请求并发时会崩溃
# （连接被 peer 关闭 → 上层表现为"空响应/任务执行一半停止"）。
# 主对话 agent 循环与 cron 任务并发调用本地模型是实际触发场景——
# 所有打到本地端口的模型请求必须串行执行。
# 引擎疑似损坏的时间戳：流被取消（停止按钮/新消息）或空响应后置位，
# 下一次本地请求在锁内先验证引擎健康，避免向残留线程竞争损坏的引擎发请求。
_llm_suspect_since: float | None = None

# 串行锁按事件循环持有：生产单循环=单锁；测试多循环互不绑定
# （模块级单一 Lock 会被首个使用它的循环绑定，跨循环抛 RuntimeError）
_stream_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _stream_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _stream_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _stream_locks[loop] = lock
    return lock


def _is_local_llm_url(api_url: str | None) -> bool:
    return bool(api_url) and ("127.0.0.1" in api_url or "localhost" in api_url)


async def _verify_llm_health(api_url: str) -> bool:
    """锁内健康验证：发一个最小生成请求，确认引擎能正常产出文本。

    注意：mlx_lm/llama server 串行处理请求——长生成期间健康请求排队超时
    是"忙"不是"死"。之前单次超时就 SIGKILL 引擎（误杀运行中的 35B 并触发
    26GB 重载→内存翻倍），现在只有端口确实死亡（连接被拒）才判死。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25)) as c:
            # model 必须用真实加载的 id：假名会让 mlx_lm.server 去 Hub 按名
            # 解析 → 镜像 SSL 校验失败 → 健康检查对健康引擎也报死。
            _model_ref = getattr(local_llm._engine, "current_model_id", "") or ""
            resp = await c.post(api_url, json={
                "model": _model_ref, "stream": False, "max_tokens": 4,
                "messages": [{"role": "user", "content": "hi"}],
            })
            resp.raise_for_status()
            data = resp.json()
            _msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
            content = (_msg.get("content") or "").strip()
            reasoning = (_msg.get("reasoning") or _msg.get("reasoning_content") or "").strip()
            # ⚠️ 推理模型（Ornith/Qwen3.8）max_tokens=4 时 token 全进 <think>，
            # content 空但 reasoning 有字——按 content 判死会把健康引擎杀了
            # 重载（与 local_llm.verify_engine_health 同样已修的坑，审计 P1）
            if not content and not reasoning:
                logger.warning("引擎返回空响应（%s），标记存疑", api_url)
                return False  # 空响应=引擎异常（端口活但产出坏了）
            return True
    except httpx.ConnectError:
        # 端口确实死亡（进程退出）→ 判死，由调用方处置
        logger.warning("本地模型引擎连接被拒（%s），判定引擎死亡", api_url)
        return False
    except Exception:
        # 超时/排队/临时错误：引擎活着但忙——不误杀
        logger.info("健康探测超时但连接可达（%s），引擎忙，视为存活", api_url)
        return True


@asynccontextmanager
async def _local_llm_serialized(api_url: str | None):
    """非流式本地请求串行化：本地 llama.cpp 时持锁，云端不设限。"""
    _local = _is_local_llm_url(api_url)
    if _local:
        await _stream_lock().acquire()
    try:
        yield
    finally:
        if _local:
            _stream_lock().release()


@asynccontextmanager
async def _local_llm_stream(client, api_url: str, body: dict, headers: dict):
    """流式请求本地/云端模型。本地 llama.cpp 时持锁直到流读完，
    防止并发流式生成导致 server 崩溃（连接被 peer 关闭）。

    引擎死亡时的恢复语义（修复"任务执行一半停止"）：
    - 有可恢复资源（模型记录在、非用户主动停止、是我们自己管理的引擎）：
      触发防重入自动重载，本函数的 5s×72 等待循环覆盖 35B 重载窗口，
      请求自然排队恢复--不再因 kill 与 reload 置位之间的竞态窗口秒死；
    - 无可恢复资源（用户手动停止 / 从未加载模型 / 外部引擎 / 重载已失败）：
      快速失败并给出明确的下一步指引。
    """
    async with _local_llm_serialized(api_url):
        global _llm_suspect_since
        engine = local_llm._engine
        _local = _is_local_llm_url(api_url)
        from urllib.parse import urlparse
        try:
            _own_engine = (urlparse(api_url).port or engine.server_port) == engine.server_port
        except Exception:
            _own_engine = True

        if _local:
            engine.mark_engine_busy()
            engine.mark_stream_enter()
        # 本流内是否已请求过重载（幂等守卫：防重载失败结束后被反复拉起）。
        # 注意不能做"本流只请求一次重载"的单发守卫：引擎可能反复挂起/404，
        # 每个循环都需要重新杀+重载（15:41 事故：单发守卫让最后一轮杀完
        # 引擎却不重载，端口空置任务永等）。防重复由 _request_reload 内部的
        # _auto_reloading 同步标志保障。
        def _request_recovery_reload() -> bool:
            if (_own_engine and engine.current_model_id
                    and not getattr(engine, "_explicit_stop", False)
                    and not engine._auto_reloading
                    and engine.server_status not in ("starting", "error")):
                return engine._request_reload(engine.current_model_id)
            return False

        if _local and _llm_suspect_since is not None:
            try:
                ok = await _verify_llm_health(api_url)
            finally:
                # enter/exit 只由本函数外层 finally 配对一次（09-05 23:31 事故：
                # 这里曾再 exit/idle 一次——suspect 路径下真实请求随之裸奔
                # （_active_local_streams=0、_engine_busy_until=0），后台健康
                # 检查对"空闲"引擎实测生成，长生成期间探测排队双连败，正忙的
                # 引擎被误判死亡杀杀重载；且外层 finally 再 exit 会把计数打成 -1。
                # 原"不配对会永久 +1"的顾虑由外层 finally 兜底）
                _llm_suspect_since = None
            if not ok:
                # 端口确实死亡或引擎产出异常--先杀残留，再触发自动重载；
                # 下面的等待-重试循环会等到引擎就绪（此前直接秒死）
                try:
                    port = urlparse(api_url).port or engine.server_port
                    engine._kill_port(port)
                    if port == engine.server_port:
                        engine.server_status = "stopped"
                except Exception:
                    pass
                if not _request_recovery_reload() and not engine._auto_reloading:
                    # 重载无法进行：区分具体原因给出准确指引（重载进行中则
                    # 落入下方等待-重试循环排队，不再秒死）
                    if getattr(engine, "_explicit_stop", False):
                        raise httpx.ConnectError(
                            "本地模型已被手动停止，任务已中断。请到模型页重新加载模型后重发消息。")
                    if engine.server_status == "error":
                        raise httpx.ConnectError(
                            f"本地模型自动重载失败（{(engine.status_message or '未知错误')[:120]}）。"
                            "请到模型页检查模型。")
                    raise httpx.ConnectError(
                        "本地模型引擎状态异常，已自动停止。请到模型页重新加载模型。"
                    )
        # 引擎短暂闪断（404/503，如 mlx_lm 高负载重启窗口）自动重试，
        # 避免整轮任务因一次瞬时不可用被判死。
        # 生成器语义：yield 之后消费者持有 r 直到读完。流中途断裂（athrow 进来
        # 的异常）后生成器不能再次 yield（asynccontextmanager 协议会破坏）--
        # 生成器内只对"连接建立即失败"（还没 yield 过）的情况重试；
        # 流中途断裂抛出明确异常，由 agent 循环的零交付重试接管。
        last_err: Exception | None = None
        _yielded = False
        # 等待恢复的总时长上限：72 次尝试本意覆盖 ~6 分钟重载窗口（每次失败
        # 连接被拒是秒级的），但引擎"挂起"（端口活、不吐响应头）时单次尝试
        # 要耗满读超时 120s——无时间上限理论上可静默拖 2.5 小时。
        _wait_deadline = time.monotonic() + 600
        # 引擎挂起判定计数：连续 2 次读超时（端口活但不吐数据）
        _hung_strikes = 0
        try:
            for _attempt in range(72):
                if time.monotonic() >= _wait_deadline:
                    raise httpx.ConnectError(
                        "等待本地模型恢复超时（10 分钟）。请到模型页检查引擎状态后重发消息。"
                    )
                try:
                    async with client.stream("POST", api_url, json=body, headers=headers) as r:
                        r.raise_for_status()  # httpx 不自动抛 4xx/5xx，必须显式检查
                        _yielded = True
                        try:
                            yield r
                        except asyncio.CancelledError:
                            if _local:
                                # 流被取消（用户点停止/发了新消息）-> 引擎生成线程可能残留
                                # 并继续跑 llama.cpp，下次请求前必须验证健康
                                _llm_suspect_since = _llm_suspect_since or time.monotonic()
                            raise
                        return
                except httpx.HTTPStatusError as e:
                    if _yielded:
                        raise  # 流中途断裂：生成器内不能重试（见函数注释）
                    _status = e.response.status_code
                    if _status in (404, 503) and _attempt < 71:
                        last_err = e
                        # 本地引擎连续 404 = 引擎状态损坏（挂起的 404 变体，
                        # 15:25 事故：模型明明加载着，迭代 2 却连续 6 分钟 404）。
                        # 健康引擎对正确路径绝不会 404——连续 2 次后杀+重载，
                        # 而不是空转 71×5s 后报"模型未就绪"。503 保持纯等待语义。
                        # ⚠️ 但 404 也可能是"模型名不匹配"：mlx_lm.server 对
                        # 未加载的 model 名（如 UI 里选中的 cloud 名 gpt-4o-mini）
                        # 也回 404，但引擎是健康的——此时绝不能杀（21:06 事故：
                        # 用户模型选了 gpt-4o-mini，本地循环发它 → 404 → 误杀
                        # 26GB 引擎重载，事件循环卡死 2 分钟）。
                        _req_model = str(body.get("model") or "")
                        _loaded = str(getattr(engine, "current_model_id", "") or "") + "|" + str(getattr(engine, "current_model_name", "") or "")
                        _model_mismatch = bool(_req_model) and _req_model not in _loaded and _req_model not in ("health-check",)
                        if (_status == 404 and _local and _own_engine
                                and _attempt >= 1 and not engine._auto_reloading
                                and engine.server_status != "starting"
                                and time.monotonic() - getattr(engine, "_engine_started_at", 0.0) > 120
                                and not _model_mismatch):
                            # 注意 starting 保护：手动加载期间 chat 接口 404 是
                            # 常态（模型未就绪），绝不能杀正在加载的引擎（P1-7）。
                            # 120s 宽限期是第二道防线：即便 "模型加载完成" 被
                            # 误报（状态已置 running 但权重还在载入，20:05 事故的
                            # 假完成），引擎启动后 2 分钟内也只等待不杀——
                            # 35B 权重加载可长达数分钟，被误杀只会再拉一轮重载。
                            logger.warning("本地引擎连续 404，判定状态损坏，强制重载")
                            try:
                                engine._kill_port(engine.server_port)
                                engine.server_status = "stopped"
                            except Exception:
                                pass
                            _request_recovery_reload()
                        await asyncio.sleep(5)  # 5s × 72 = 最大约 6 分钟等待
                        continue
                    raise
                except httpx.TransportError as e:
                    # TransportError = ConnectError/ReadError/WriteError/
                    # RemoteProtocolError/各超时的共同基类。引擎被杀可能表现为
                    # 其中任何一种（实测 kill -9 在预填充期抛 ReadError）。
                    if _yielded:
                        # 流中途断裂（引擎被杀/系统压力/网络断）：生成器内不能重发，
                        # 抛出明确语义的异常，由 agent 循环的零交付重试接管
                        raise httpx.RemoteProtocolError(
                            "本地模型流中断（引擎死亡或连接断开）。"
                            "若本轮尚无输出，任务将自动等待引擎恢复并重试。"
                        ) from e
                    if _local:
                        # 挂起检测：读超时/读错误 = 端口活着但不吐数据；连接超时
                        # 同样可能（引擎 accept 积压已满）。连续 2 次且没有其他
                        # 活跃流（=没有别的请求在生成长文本）→ 判定挂起，杀掉重载。
                        # 有其他活跃流时可能是排队等长生成，不动（A3 忙保护）。
                        if (isinstance(e, (httpx.ReadError, httpx.TimeoutException))
                                and engine._active_local_streams <= 1):
                            _hung_strikes += 1
                            if (_hung_strikes >= 2 and not engine._auto_reloading
                                    and engine.current_model_id
                                    and not getattr(engine, "_explicit_stop", False)
                                    and engine.server_status != "error"):
                                logger.warning(
                                    "引擎端口存活但连续读超时且无其他活跃流，判定挂起，强制重载")
                                try:
                                    engine._kill_port(engine.server_port)
                                    engine.server_status = "stopped"
                                except Exception:
                                    pass
                                _request_recovery_reload()
                        # 判断有无恢复资源，决定快速失败还是排队等重载。
                        # 此前只要 _auto_reloading 未置位就秒死，而引擎几秒后
                        # 就能自动恢复（kill 与 reload 置位之间的竞态窗口）。
                        if not _own_engine:
                            raise httpx.ConnectError(
                                "外部模型引擎（LM Studio/Ollama）未运行。请启动外部引擎后重试。"
                            ) from e
                        if getattr(engine, "_explicit_stop", False):
                            raise httpx.ConnectError(
                                "本地模型已被手动停止，任务已中断。请到模型页重新加载模型后重发消息。"
                            ) from e
                        _load_in_progress = engine.server_status == "starting"
                        if not engine.current_model_id and not _load_in_progress:
                            raise httpx.ConnectError(
                                "本地模型引擎未运行（端口无监听）。请到模型页加载模型。"
                            ) from e
                        # 有恢复资源：确保重载已触发（幂等，防重入）。
                        # 重载请求被拒且状态是 error = 上一次重载已失败收场，
                        # 快速失败并给出原因，不再无限等待。
                        if not engine._auto_reloading:
                            _started = _request_recovery_reload()
                            if not _started and engine.server_status == "error":
                                raise httpx.ConnectError(
                                    f"本地模型自动重载失败（{(engine.status_message or '未知错误')[:120]}）。"
                                    "请到模型页检查模型。"
                                ) from e
                    # 重载窗口期端口无进程（连接被拒）或读超时/连接重置：
                    # 等待重试（自动重载完成即恢复）
                    if _attempt < 71:
                        last_err = e
                        await asyncio.sleep(5)
                        continue
                    raise
            if last_err is not None:
                raise last_err
        finally:
            if _local:
                engine.mark_stream_exit()
                engine.mark_engine_idle()

