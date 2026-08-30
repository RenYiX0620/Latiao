#!/usr/bin/env python3
"""控制类工具共享逻辑（私有模块，带 _ 前缀不会被当作插件加载）。"""
import os
import subprocess
import sys
import time
from pathlib import Path

RUNLOGS = Path.home() / ".local-ai-os" / "runlogs"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def self_pids() -> set[str]:
    """当前进程树 pid：sidecar python 与 Latiao 应用本体，杀这些会被拒绝。"""
    pids = {str(os.getpid())}
    try:
        pids.add(str(os.getppid()))
    except Exception:
        pass
    return pids


def safe_name(name: str) -> str:
    return (name or "").strip()


def ps_table(pattern: str = "") -> list[str]:
    """ps 输出解析为行列表（pid cpu mem comm），MAC/Linux 通用。"""
    r = subprocess.run(
        ["ps", "-axo", "pid=,pcpu=,pmem=,comm="],
        capture_output=True, text=True, timeout=15,
    )
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, cpu, mem, comm = parts[0], parts[1], parts[2], parts[3]
        if pattern and pattern.lower() not in comm.lower():
            continue
        rows.append(f"pid={pid} cpu={cpu}% mem={mem}% {comm}")
    return rows


def tasklist_table(pattern: str = "") -> list[str]:
    """Windows tasklist 解析。"""
    r = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15)
    out = []
    for l in r.stdout.splitlines():
        parts = l.strip().strip('"').split('","')
        if len(parts) >= 2:
            name, pid = parts[0], parts[1]
            if pattern and pattern.lower() not in name.lower():
                continue
            out.append(f"pid={pid} {name}")
    return out


def kill_pid(pid: int) -> str:
    if pid <= 1:
        return f"⛔ 拒绝杀死 pid={pid}：系统关键进程"
    if str(pid) in self_pids():
        return f"⛔ 拒绝杀死 pid={pid}：这是 Latiao/Sidecar 自身进程"
    try:
        os.kill(pid, 15)
        return f"✅ 已发送终止信号（SIGTERM）给 pid={pid}"
    except ProcessLookupError:
        return f"进程 pid={pid} 不存在（可能已退出）"
    except PermissionError:
        return f"⛔ 无权限杀死 pid={pid}（可能需要 sudo）"
    except Exception as e:
        return f"杀死失败: {e}"


def kill_by_name(pattern: str) -> str:
    try:
        if is_windows():
            rows = tasklist_table(pattern)
            targets = [r for r in rows if pattern.lower() in r.lower() and "pid=" in r]
            if not targets:
                return f"没有找到名为 {pattern!r} 的进程"
            return f"Windows 建议按 pid 精确终止；匹配 {len(targets)} 个进程请用 pid"
        r = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=15)
        targets = []
        for line in r.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            p, comm = parts[0], parts[1]
            if pattern.lower() in comm.lower() or os.path.basename(comm).lower() == pattern.lower():
                if p not in self_pids() and int(p) > 1:
                    targets.append((p, comm))
        if not targets:
            return f"没有找到名为 {pattern!r} 的进程"
        killed = 0
        for p, comm in targets[:20]:
            try:
                os.kill(int(p), 15)
                killed += 1
            except Exception:
                pass
        return f"✅ 已终止 {killed}/{len(targets)} 个匹配 {pattern!r} 的进程"
    except Exception as e:
        return f"按名称杀死失败: {e}"


def launch_bg(command: str) -> str:
    if not command or len(command) > 2000:
        return "❌ command 不能为空且长度 ≤2000"
    RUNLOGS.mkdir(parents=True, exist_ok=True)
    tag = f"proc_{int(time.time())}"
    out_path = RUNLOGS / f"{tag}.out"
    err_path = RUNLOGS / f"{tag}.err"
    try:
        import shlex
        tokens = shlex.split(command)
        if not tokens:
            return "❌ 命令无法解析"
        with open(out_path, "w", encoding="utf-8"), open(err_path, "w", encoding="utf-8") as err_f:
            proc = subprocess.Popen(
                tokens, stdout=open(out_path, "w"), stderr=err_f,
                start_new_session=True, stdin=subprocess.DEVNULL,
            )
        return (
            f"✅ 已启动: pid={proc.pid}（{command}）\n"
            f"输出文件: {out_path}\n错误文件: {err_path}\n"
            f"后续可用 control_process_log(action=read, pid={proc.pid}) 查看输出"
        )
    except FileNotFoundError as e:
        return f"❌ 命令不存在: {e}"
    except Exception as e:
        return f"❌ 启动失败: {e}"


def read_bg_log(lines: int = 50) -> str:
    lines = max(1, min(200, int(lines or 50)))
    try:
        if not RUNLOGS.exists():
            return "暂无后台进程输出"
        files = sorted(RUNLOGS.glob("*.out"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return "暂无后台进程输出"
        f = files[0]
        data = f.read_text(encoding="utf-8", errors="ignore")
        all_lines = data.splitlines()
        tail = all_lines[-lines:]
        return f"后台输出（{f.name}，共 {len(all_lines)} 行，显示尾部 {len(tail)} 行）：\n" + "\n".join(tail)
    except Exception as e:
        return f"读取输出失败: {e}"
