"""匿名使用数据（2026-10-11 立项，用户拍板："设计他就是为了用他的"）。

**背景**：设置页的"匿名使用数据"开关此前是个假开关——前端只写 localStorage，
全项目无消费方。本模块把收集侧做成真的，并给出一个诚实的边界：

- **只收集计数，不收集内容**。白名单字段，没有自由文本：会话/轮次/工具调用
  计数、错误分类计数（哪类闸门触发多）、模型档位（local/cloud 二分，不记模型名）。
  聊天内容、文件路径、工具参数与结果、模型文件名、密钥——一律不落。
- **安装 ID** 随机生成（`latiao_<32hex>`），与任何个人信息无关；重置只需删一行。
- **开关是总闸**：`privacy.anonymous_usage` = false 时 collect/send 全部短路。
  关掉不会删除已收集的本地数据（Trash 文化：只停止收集，不偷偷清账）。
- **发送端默认不配**。`LATIAO_ANON_ENDPOINT`（环境变量）或 config.json
  `privacy.anon_endpoint` 没设时，数据只在**本机**累积、永不外发——这是诚实
  降级而不是第二层假开关：数据真的在涨，设置页能看到条数；要它出门只差一行配置。
- **频率**：每条事件只更新日聚合，不排队原始事件；发送走日聚合快照，每 24h
  最多一次（`send_if_due`），失败静默保留（下次再试）。

存储：`~/.local-ai-os/anon_usage.json`（0600，原子写）。上限 400 天；写失败
只 warn——记账绝不能影响业务（与 turn_metrics 同一取舍）。
"""
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone

from config import CONFIG_FILE, PROGRESS_DIR

logger = logging.getLogger("latiao-sidecar")

ANON_FILE = PROGRESS_DIR / "anon_usage.json"
_SCHEMA = 1
_KEEP_DAYS = 400
_SEND_INTERVAL_SEC = 24 * 3600

# 事件白名单：只有这些 key 会进日聚合。新增指标必须在这里登记，
# 防止将来有人顺手塞进自由文本（"只收集计数"是承诺，不是默认）。
_COUNTER_KEYS = (
    "sessions_started",     # 新会话
    "turns",                # 完成的轮次
    "turns_error",          # 以错误收尾的轮次
    "turns_aborted",        # 用户中断的轮次
    "tool_calls",           # 工具调用次数
    "tool_errors",          # 工具失败次数
    "gate_idle_nudge",      # 空转闸门：催促
    "gate_force_final",     # 空转/停滞闸门：强制收口
    "gate_fragment",        # 交付碎片哨兵触发
    "gate_trace",           # 溯源哨兵（回答里有无出处的数字）
    "gate_lang",            # 语言闸门修正
    "gate_reflection",      # 输出反思修正（light/deep）
    "ctx_fold",             # 工具输出回收（折叠）
)

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    """读本机匿名数据文件（惰性、带缓存）。坏文件当作空账本，不抛。"""
    global _cache
    if _cache is not None:
        return _cache
    data: dict = {"schema": _SCHEMA, "install_id": "", "days": {}, "last_sent": ""}
    try:
        if ANON_FILE.exists():
            raw = json.loads(ANON_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: raw.get(k, data.get(k)) for k in data})
                if not isinstance(data.get("days"), dict):
                    data["days"] = {}
    except Exception:
        logger.warning("anon_usage: 本机数据读取失败（按空账本继续）", exc_info=True)
    _cache = data
    return data


def _save(data: dict) -> bool:
    """原子写 + 0600（与 config.save_config 同一套做法）。"""
    try:
        ANON_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ANON_FILE.with_name(ANON_FILE.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        try:
            os.chmod(str(tmp), 0o600)
        except OSError:
            pass
        os.replace(str(tmp), str(ANON_FILE))
        return True
    except Exception:
        logger.warning("anon_usage: 本机数据写入失败（不影响业务）", exc_info=True)
        return False


def _cfg_privacy() -> dict:
    """config.json 的 privacy 段（缺失/坏文件 → 空 dict）。"""
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            sec = cfg.get("privacy")
            if isinstance(sec, dict):
                return sec
    except Exception:
        logger.debug("anon_usage: config privacy 读取失败", exc_info=True)
    return {}


def tracking_enabled() -> bool:
    """总闸：开关关 → 收集与发送全部短路。

    读取顺序：环境变量 `LATIAO_ANON_USAGE=0/1`（测试与排障用）> config.json
    `privacy.anonymous_usage` > 默认开（与前端 localStorage 的默认一致——
    开关 UI 默认是勾上的，后端必须同口径，否则"勾着却不收集"又成假开关）。
    """
    env = os.environ.get("LATIAO_ANON_USAGE")
    if env is not None:
        return env.strip() not in ("0", "false", "False", "no")
    cfg = _cfg_privacy()
    if "anonymous_usage" in cfg:
        return bool(cfg.get("anonymous_usage"))
    return True


def endpoint() -> str:
    """上报地址：环境变量 > config.json。空串 = 只本机累积、永不外发。"""
    env = os.environ.get("LATIAO_ANON_ENDPOINT", "").strip()
    if env:
        return env
    return str(_cfg_privacy().get("anon_endpoint", "") or "").strip()


def install_id() -> str:
    """随机安装 ID（无任何个人信息）。首次调用时生成并落盘。"""
    with _lock:
        data = _load()
        if not data.get("install_id"):
            data["install_id"] = "latiao_" + secrets.token_hex(16)
            _save(data)
        return str(data["install_id"])


def set_enabled(enabled: bool) -> dict:
    """写开关到 config.json（设置页唯一入口；前端 localStorage 仍并存）。"""
    from config import save_config
    cfg: dict = {}
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("anon_usage: config 读取失败，开关按新建写入", exc_info=True)
    privacy = cfg.get("privacy")
    if not isinstance(privacy, dict):
        privacy = {}
    privacy["anonymous_usage"] = bool(enabled)
    cfg["privacy"] = privacy
    save_config(cfg)
    logger.info("匿名使用数据开关：%s", "开" if enabled else "关")
    return {"status": "ok", "enabled": bool(enabled)}


def record_event(key: str, n: int = 1) -> None:
    """记一条计数（白名单外静默丢弃）。开关关时直接返回。"""
    if key not in _COUNTER_KEYS:
        logger.debug("anon_usage: 未登记的事件 key=%r 已丢弃", key)
        return
    if not tracking_enabled():
        return
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with _lock:
            data = _load()
            days = data.setdefault("days", {})
            day = days.setdefault(today, {})
            day[key] = int(day.get(key, 0)) + int(n)
            _prune(days)
            _save(data)
    except Exception:
        logger.debug("anon_usage: record_event 失败", exc_info=True)


def note(key: str, n: int = 1) -> None:
    """插桩快捷入口：任何失败都吞掉（闸门/工具路径调用它绝不许出异常）。"""
    try:
        record_event(key, n)
    except Exception:
        logger.debug("anon_usage: note(%r) 失败", key, exc_info=True)


def _prune(days: dict) -> None:
    if len(days) <= _KEEP_DAYS:
        return
    for k in sorted(days)[:-_KEEP_DAYS]:
        days.pop(k, None)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def status() -> dict:
    """本机状态（设置页展示 + 测试断言用）。"""
    data = _load()
    days = data.get("days", {})
    total = sum(sum(int(v) for v in d.values()) for d in days.values() if isinstance(d, dict))
    return {
        "enabled": tracking_enabled(),
        "endpoint_configured": bool(endpoint()),
        "install_id": str(data.get("install_id", ""))[:15] + "…" if data.get("install_id") else "",
        "events_total": total,
        "days_tracked": len(days),
        "last_sent": str(data.get("last_sent", "")),
        "local_file": str(ANON_FILE),
    }


def _payload() -> dict:
    """上报快照：只有版本、平台与计数。"""
    # 走 install_id() 而不是直接读 data：ID 是惰性生成的，这里不生成会出现
    # 首报带空 install_id（测试 test_endpoint_from_config_and_send_due_window 抓到）。
    data = _load()
    ver = os.environ.get("LATIAO_APP_VERSION", "")
    return {
        "schema": _SCHEMA,
        "install_id": install_id(),
        "app_version": ver,
        "platform": f"{os.uname().sysname} {os.uname().machine}" if hasattr(os, "uname") else "",
        "days": data.get("days", {}),
    }


def send_if_due(force: bool = False) -> dict:
    """到了 24h 口径就把日聚合快照发去 endpoint；没配端点/开关关 → 不发。

    失败只记状态不发愁：数据留在本机，下次再试（离线不该影响任何事）。
    """
    if not tracking_enabled():
        return {"sent": False, "reason": "disabled"}
    url = endpoint()
    if not url:
        return {"sent": False, "reason": "no_endpoint"}
    now = datetime.now(timezone.utc)
    with _lock:
        data = _load()
        last = str(data.get("last_sent", ""))
        if not force and last:
            try:
                last_dt = datetime.fromisoformat(last)
                if (now - last_dt).total_seconds() < _SEND_INTERVAL_SEC:
                    return {"sent": False, "reason": "not_due"}
            except ValueError:
                pass
    try:
        import httpx
        with httpx.Client(timeout=15.0) as c:
            resp = c.post(url, json=_payload())
        ok = 200 <= resp.status_code < 300
    except Exception as e:
        logger.info("匿名数据上报失败（留本机，稍后再试）：%s", type(e).__name__)
        return {"sent": False, "reason": "error"}
    if ok:
        with _lock:
            data = _load()
            data["last_sent"] = now.isoformat(timespec="seconds")
            _save(data)
        logger.info("匿名数据已上报（%d 天聚合）", len(_load().get("days", {})))
    return {"sent": bool(ok), "reason": "ok" if ok else "http_error"}


def reset() -> dict:
    """清空本机匿名数据（含安装 ID）。设置页的"清除本机数据"入口未来用。"""
    global _cache
    with _lock:
        _cache = {"schema": _SCHEMA, "install_id": "", "days": {}, "last_sent": ""}
        _save(_cache)
    return {"status": "ok"}
