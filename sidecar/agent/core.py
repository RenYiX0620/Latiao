"""Scope 容器（对标 dsh/Cordis「万物皆插件」）。

架构原则：
- host 平面（引擎传输、session 事件日志、权限、模型路由）全局唯一；
  agent 平面 = 每个会话/子代理一个 Scope，挂载自己的工具目录、prompt 段、钩子。
- 一切能力都是插件：setup(ctx) 向 Scope 注册 tools / sections / waterfall 钩子。
- fail-loud：同名工具重复注册且定义不同 → 立即抛错，不做静默覆盖。
- 工具目录 agent-scoped：子 Scope 的 restrict 后工具既不在提示词、执行也被拒。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("latiao-sidecar")


class ScopeError(Exception):
    """Scope 注册/组合冲突（fail-loud）。"""


class Waterfall:
    """有序异步钩子链：钩子返回改写后的载荷；返回 None 表示不修改。

    语义对齐 dsh：agent/pre-step（改写消息或 reject）、agent/request
    （改写模型/effort/max_tokens）、agent/turn-stopping（收尾钩子）。
    """

    def __init__(self, name: str):
        self.name = name
        self._hooks: list[tuple[int, str, Callable[..., Awaitable[Any]]]] = []

    def register(self, hook: Callable[..., Awaitable[Any]], *, order: int = 0,
                 name: str = "") -> None:
        if not callable(hook):
            raise ScopeError(f"waterfall[{self.name}] hook 必须可调用")
        self._hooks.append((order, name or getattr(hook, "__name__", "hook"), hook))
        self._hooks.sort(key=lambda h: h[0])

    def unregister(self, name: str) -> None:
        before = len(self._hooks)
        self._hooks = [h for h in self._hooks if h[1] != name]
        if len(self._hooks) != before:
            logger.debug("waterfall[%s] unregistered hook %s", self.name, name)

    async def run(self, payload: Any, ctx: Any = None) -> Any:
        for _order, hname, hook in self._hooks:
            try:
                out = await hook(payload, ctx)
            except Exception:
                logger.exception("waterfall[%s] hook %s 抛错（fail-loud 透传）",
                                 self.name, hname)
                raise
            if out is not None:
                payload = out
        return payload

    def hooks(self) -> list[str]:
        return [h[1] for h in self._hooks]


WATERFALLS = ("pre_step", "request", "turn_stopping")


class Scope:
    """一个 agent 平面组合：工具目录 + prompt 段 + waterfall + 服务。

    child() 派生子 scope（子代理/委派）：继承父工具目录后再 restrict，
    persona/prompt 段可遮蔽，执行拒绝在 tools 层强制。"""

    def __init__(self, name: str = "root", parent: "Scope | None" = None):
        self.name = name
        self.parent = parent
        self._tools: dict[str, dict] = {}
        self._dispatch: dict[str, Callable] = {}
        self._permissions: dict[str, str] = {}
        self._restricted: set[str] | None = None  # None = 不限制
        self._sections: dict[str, tuple[int, str]] = {}  # id -> (order, text)
        self._waterfalls: dict[str, Waterfall] = {w: Waterfall(w) for w in WATERFALLS}
        self._services: dict[str, Any] = {}
        self._children: list["Scope"] = []

    # ── 工具目录（agent-scoped）────────────────────────────
    def register_tool(self, tool: dict, dispatch: Callable | None = None,
                      permission: str = "safe") -> None:
        name = (tool.get("function") or {}).get("name", "")
        if not name:
            raise ScopeError("工具定义缺少 function.name")
        existing = self._tools.get(name)
        if existing is not None and existing != tool:
            raise ScopeError(f"工具 {name} 重复注册且定义不同（fail-loud）")
        if dispatch is not None:
            existing_dispatch = self._dispatch.get(name)
            if existing_dispatch is not None and existing_dispatch is not dispatch:
                raise ScopeError(f"工具 {name} 重复注册且 dispatch 不同（fail-loud）")
            self._dispatch[name] = dispatch
        self._permissions.setdefault(name, permission)

    def tools(self) -> list[dict]:
        """生效工具目录（restrict 过滤后）。"""
        out = []
        for name, tool in self._tools.items():
            if self._restricted is not None and name not in self._restricted:
                continue
            out.append(tool)
        return out

    def dispatch_for(self, name: str) -> Callable | None:
        if self._restricted is not None and name not in self._restricted:
            return None  # restrict 后执行被拒（与提示词消失同口径）
        return self._dispatch.get(name)

    def permission_for(self, name: str) -> str:
        return self._permissions.get(name, "safe")

    def restrict_tools(self, allowed: set[str]) -> None:
        """子代理隔离：只保留 allowed 集内的工具（提示词消失 + 执行拒绝）。"""
        self._restricted = set(allowed)

    # ── prompt 段 ─────────────────────────────────────────
    def register_section(self, sid: str, text: str, *, order: int = 0) -> None:
        self._sections[sid] = (order, text)

    def sections(self) -> str:
        ordered = sorted(self._sections.values(), key=lambda t: t[0])
        return "\n\n".join(t for _o, t in ordered)

    # ── waterfall ─────────────────────────────────────────
    def waterfall(self, name: str) -> Waterfall:
        if name not in self._waterfalls:
            raise ScopeError(f"未知 waterfall: {name}（可用: {list(self._waterfalls)}）")
        return self._waterfalls[name]

    async def run_waterfall(self, name: str, payload: Any) -> Any:
        return await self.waterfall(name).run(payload, self)

    # ── 服务 ──────────────────────────────────────────────
    def provide(self, key: str, obj: Any) -> None:
        self._services[key] = obj

    def get_service(self, key: str, default: Any = None) -> Any:
        if key in self._services:
            return self._services[key]
        if self.parent is not None:
            return self.parent.get_service(key, default)
        return default

    # ── 派生 ──────────────────────────────────────────────
    def child(self, name: str) -> "Scope":
        child = Scope(name=name, parent=self)
        # 工具目录继承父（含父的 restrict 结果），子可再 restrict 收窄
        child._tools = dict(self.tools_by_name())
        child._dispatch = dict(self._dispatch)
        child._permissions = dict(self._permissions)
        if self._restricted is not None:
            child._restricted = set(self._restricted)
        self._children.append(child)
        return child

    def tools_by_name(self) -> dict[str, dict]:
        if self._restricted is not None:
            return {n: t for n, t in self._tools.items() if n in self._restricted}
        return dict(self._tools)
