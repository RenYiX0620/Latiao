"""自动验证（agent_loop.py 拆出的第五块，2026-09-23）。

工具执行后的自检：回读文件 / 语法检查 / 语义层扫描（semgrep 可选）。与执行器解耦——
它只吃 (tool_name, args, result)，吐一段给模型看的验证文本。
"""
import logging

import asyncio
import re
from cmd_safety import child_env
from pathlib import Path

logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

# 自动验证要跑 npx/tsc/eslint/git/semgrep：这些工具自己的配置前缀保留，
# 但不该拿到 sidecar token 与云模型密钥（安全批次 ④，2026-09-23）
_VERIFY_ENV_PREFIXES = ("NODE_", "NPM_", "npm_", "SEMGREP_", "GIT_", "XDG_")

async def _auto_verify(tool_name: str, args: dict, result: str) -> str:
    """Run programmatic verification after a tool executes.
    Returns a verification report to inject into the LLM context, or '' if nothing to verify."""
    checks = []
    path = ''

    if tool_name == "write_file":
        path = args.get("path") or args.get("file") or ""
        content_written = args.get("content") or ""

        # ── Read-back verification ──
        if path:
            try:
                loop = asyncio.get_running_loop()
                actual = await loop.run_in_executor(None, lambda: Path(path).read_text(encoding="utf-8"))
                if actual == content_written:
                    checks.append(("OK", "回读比对", f"内容一致 ({len(content_written)} 字符)"))
                else:
                    diff = len(actual) - len(content_written)
                    checks.append(("FAIL", "回读比对", f"内容不一致！期望 {len(content_written)} 字符，实际 {len(actual)} (差 {diff})"))
                lines = actual.split("\n")
                checks.append(("OK", "完整性", f"{len(lines)} 行, 首行: {lines[0][:60] if lines else '(空)'}"))
            except FileNotFoundError:
                checks.append(("FAIL", "文件存在", f"写入后文件不存在: {path}"))

        # ── TypeScript type-check (find nearest tsconfig.json) ──
        if path.endswith((".ts", ".tsx")):
            p = Path(path)
            for parent in [p.parent, p.parent.parent, p.parent.parent.parent]:
                if (parent / "tsconfig.json").exists():
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "npx", "tsc", "--noEmit", cwd=str(parent),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            env=child_env(allow_prefixes=_VERIFY_ENV_PREFIXES),
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                        if proc.returncode == 0:
                            checks.append(("OK", "TS 类型检查", "tsc --noEmit 通过"))
                        else:
                            output = (stderr or stdout or b"").decode("utf-8", errors="replace")
                            errs = [line for line in output.strip().split("\n") if line.strip()]
                            checks.append(("FAIL", "TS 类型检查", f"发现 {len(errs)} 个错误"))
                            for el in errs[:3]:
                                checks.append(("  ", "  ↳", el[:120]))
                    except FileNotFoundError:
                        pass
                    except asyncio.TimeoutError:
                        checks.append(("FAIL", "TS 类型检查", "超时"))
                    except Exception:
                        logger.warning("TypeScript check failed in auto-verify", exc_info=True)
                    break

    if tool_name == "run_cmd":
        exit_match = re.search(r'退出码:\s*(\d+)', result)
        if exit_match:
            code = int(exit_match.group(1))
            checks.append(("OK" if code == 0 else "FAIL", "退出码", f"exit {code}"))
        elif "超时" in result:
            checks.append(("FAIL", "超时", "命令执行超时 (30s)"))

    if tool_name in ("write_file", "run_cmd"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--stat",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=child_env(allow_prefixes=_VERIFY_ENV_PREFIXES),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0 and stdout.strip():
                checks.append(("INFO", "Git 变更", "\n" + stdout.decode("utf-8", errors="replace").strip()))
        except Exception:
            logger.warning("Git diff check failed in auto-verify", exc_info=True)

        # ── ESLint check for JS/TS files ──
        if path.endswith((".ts", ".tsx", ".js", ".jsx")):
            p = Path(path)
            for parent in [p.parent, p.parent.parent, p.parent.parent.parent]:
                if (parent / "eslint.config.js").exists() or (parent / ".eslintrc").exists():
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            "npx", "eslint", str(p),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                            env=child_env(allow_prefixes=_VERIFY_ENV_PREFIXES),
                        )
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                        output = (stdout or stderr or b"").decode("utf-8", errors="replace").strip()
                        if proc.returncode == 0 and not output:
                            checks.append(("OK", "ESLint", "无警告"))
                        elif output:
                            errs = [line for line in output.split("\n") if line.strip()][:5]
                            checks.append(("FAIL", "ESLint", f"发现 {len(errs)} 个问题"))
                            for el in errs[:3]:
                                checks.append(("  ", "  ↳", el[:120]))
                    except FileNotFoundError:
                        pass
                    except asyncio.TimeoutError:
                        checks.append(("FAIL", "ESLint", "超时"))
                    except Exception:
                        logger.debug("ESLint check failed", exc_info=True)
                    break

        # ── Python syntax check ──
        if path.endswith(".py"):
            try:
                try:
                    with open(path, encoding="utf-8") as _f:
                        source = _f.read()
                    compile(source, path, "exec")
                    checks.append(("OK", "Python 语法", "编译通过"))
                except SyntaxError as _e:
                    checks.append(("FAIL", "Python 语法", str(_e)[:150]))
            except FileNotFoundError:
                pass
            except asyncio.TimeoutError:
                checks.append(("FAIL", "Python 语法", "超时"))
            except Exception:
                logger.debug("Python syntax check failed", exc_info=True)

    # ── Semgrep security scan ──
    await _enhance_auto_verify(tool_name, args, result, checks)

    if not checks:
        return ""

    report = ["\n## 🔍 自动验证"]
    all_ok = all(s in ("OK", "INFO", "  ") for s, _, _ in checks)
    report.append(f"**{'✅ 全部通过' if all_ok else '⚠️ 发现问题'}**\n")
    for status, name, detail in checks:
        icon = {"OK": "✅", "FAIL": "❌", "INFO": "📋", "  ": "  "}.get(status, status)
        report.append(f"- {icon} **{name}**: {detail}")
    return "\n".join(report)

async def _enhance_auto_verify(tool_name: str, args: dict, result: str, checks: list):
    """Add semgrep scanning to the verification checks list."""
    if tool_name != "write_file":
        return
    path = args.get("path") or args.get("file") or ""
    if not path.endswith((".ts", ".tsx", ".js", ".jsx", ".py")):
        return
    scan_result = await _semgrep_scan(path)
    if scan_result:
        issue_count = scan_result.count("\n") + 1
        checks.append(("FAIL" if "error" in scan_result.lower() else "OK",
                       "Semgrep 安全扫描",
                       f"发现 {issue_count} 行输出" if issue_count > 1 else "通过"))
        if issue_count > 1:
            for line in scan_result.split("\n")[:3]:
                if line.strip():
                    checks.append(("  ", "  ↳", line[:120]))
    else:
        checks.append(("OK", "Semgrep", "跳过 (未安装或无可扫描内容)"))

async def _semgrep_scan(filepath: str) -> str | None:
    """Run semgrep on a file if available. Returns scan report or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "semgrep", "--config", "auto", "--quiet", filepath,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=child_env(allow_prefixes=_VERIFY_ENV_PREFIXES),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        output += (stderr or b"").decode("utf-8", errors="replace").strip()
        if output:
            return output
        return None
    except FileNotFoundError:
        return None  # semgrep not installed
    except asyncio.TimeoutError:
        return "semgrep 扫描超时"
    except Exception:
        logger.debug("Semgrep scan failed", exc_info=True)
        return None
