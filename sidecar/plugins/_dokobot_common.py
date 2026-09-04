"""Dokobot 共享逻辑：真实浏览器渲染读取/搜索（绕反爬、可带登录态）。

依赖：本机 Dokobot CLI（npm i -g @dokobot/cli）+ Chrome 扩展 + install-bridge。
未安装时返回明确的安装指引；浏览器未检测到时同样提示。
"""
import os
import shutil
import subprocess
from pathlib import Path

_NOT_INSTALLED = (
    "⚠️ Dokobot 未安装或浏览器桥接未配置。启用步骤：\n"
    "1. npm i -g @dokobot/cli\n"
    "2. dokobot install-bridge（需要已安装 Chrome/Edge/Brave）\n"
    "3. 浏览器安装 Dokobot 扩展（dokobot.ai / Chrome 商店）\n"
    "配好后本工具即可读取反爬/动态渲染页面。"
)


def dokobot_bin() -> str | None:
    """定位 dokobot CLI（兼容 nvm 安装路径不在 sidecar PATH 的情况）。"""
    p = shutil.which("dokobot")
    if p:
        return p
    home = Path.home()
    for cand in sorted((home / ".nvm" / "versions" / "node").glob("*/bin/dokobot"), reverse=True):
        return str(cand)
    return None


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    env = dict(os.environ)
    node_bin = Path.home() / ".nvm" / "versions" / "node"
    if node_bin.exists():
        import glob as _g
        found = _g.glob(str(node_bin) + "/*/bin")
        if found:
            env["PATH"] = env.get("PATH", "") + ":" + ":".join(found)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    out = (r.stdout or "")
    if r.stderr and r.stderr.strip():
        out += "\n[stderr] " + r.stderr
    return r.returncode, out
