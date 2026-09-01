/**
 * 应用自动更新（v2：sidecar 预下载代理——根治大文件断流）。
 *
 * 流程：
 *   1. invoke sidecar /v1/update/prepare → sidecar 后台断点续传下载安装包
 *      （Range 续传 + 重试 + 状态落盘，跨重启存活；本地轮询显示真实百分比）
 *   2. 预下载 done → tauri updater check()（endpoint 指向 sidecar 本地清单）
 *      + downloadAndInstall()（从 127.0.0.1 秒下 + 验签 + 静默安装）→ relaunch
 *   3. sidecar 不可用/预下载失败 → 回退 tauri updater 原路径兜底
 */
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import { getVersion } from "@tauri-apps/api/app";
import { authFetch } from "./api";

export async function getAppVersion(): Promise<string> {
  try {
    return await getVersion();
  } catch {
    return "0.3.4";
  }
}

export type UpdateCheckResult = "none" | "error" | "installed" | "prepared";

async function sidecarPrepare(version: string): Promise<boolean> {
  try {
    await authFetch("/v1/update/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_version: version }),
    });
    return true;
  } catch {
    return false; // sidecar 不可用 → 回退直连
  }
}

/** 轮询预下载进度直到终态。返回终态或 null（sidecar 掉线/超时）。 */
async function pollPreDownload(
  onProgress: (msg: string) => void,
  maxMs: number,
): Promise<"done" | "failed" | "up_to_date" | "idle" | null> {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    try {
      const resp = await authFetch("/v1/update/progress");
      const data = await resp.json();
      const p = data?.progress || {};
      const total = p.total || 0;
      const downloaded = p.downloaded || 0;
      if (p.status === "downloading" && total > 0) {
        const pct = Math.floor((downloaded / total) * 100);
        const mb = (downloaded / 1024 / 1024).toFixed(0);
        const mbt = (total / 1024 / 1024).toFixed(0);
        onProgress(`正在下载新版本 ${p.version}… ${pct}%（${mb}/${mbt} MB，断线自动续传）`);
      }
      if (p.status === "done") return "done";
      if (p.status === "failed") return "failed";
      if (p.status === "up_to_date") return "up_to_date";
      // checking/idle：继续等（预下载在 sidecar 后台跑，跨重启续传）
    } catch {
      return null; // sidecar 掉线 → 回退
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  return null;
}

/**
 * 检查更新并安装。
 * - interactive=true（用户点按钮）：等待预下载完成（显示百分比）→ 安装 → relaunch
 * - interactive=false（启动静默）：只触发后台预下载（最多陪跑 90 秒），不阻塞——
 *   预下载完成后下次手动检查或重启时会安装
 */
export async function checkForUpdates(
  onStatus: (msg: string) => void,
  interactive = true,
): Promise<UpdateCheckResult> {
  try {
    const version = await getAppVersion();
    const waitMs = interactive ? 60 * 60 * 1000 : 90 * 1000;
    let outcome: "done" | "failed" | "up_to_date" | "idle" | null = null;

    // ── 阶段 1：sidecar 预下载（断点续传，根治断流）──
    const prepared = await sidecarPrepare(version);
    if (prepared) {
      outcome = await pollPreDownload(onStatus, waitMs);
      if (outcome === "up_to_date") {
        if (interactive) onStatus("当前已是最新版本");
        return "none";
      }
      if (outcome === "done") {
        onStatus("下载完成，正在校验签名并安装…");
      }
      // failed / null → 落到下方 tauri updater 兜底
    }

    // ── 阶段 2：tauri updater（endpoint 指向 sidecar 本地清单；验签+安装不变）──
    const update = await check();
    if (!update) {
      if (prepared && outcome === null) {
        // sidecar 掉线回退：endpoint 仍指向 sidecar → 清单不可得
        onStatus("更新检查不可用（sidecar 未运行），请稍后重试或手动下载");
        return "error";
      }
      if (interactive) onStatus("已是最新版本");
      return "none";
    }
    onStatus(`发现新版本 ${update.version}，正在安装…`);
    // 静默预下载模式只下不装：启动时自动安装并重启会在用户正聊天时
    // 强制退出（审计 P1）。下载安装/重启仅限用户显式点「检查更新」。
    if (!interactive) {
      onStatus("更新包已准备好，下次手动检查或稍后重启时安装");
      return "prepared";
    }
    let finished = false;
    await update.downloadAndInstall((ev) => {
      if (ev.event === "Started") {
        onStatus("校验签名完成，开始安装…");
      } else if (ev.event === "Finished") {
        finished = true;
        onStatus("更新已安装，即将重启应用…");
      }
    });
    if (finished) {
      setTimeout(() => {
        relaunch().catch(() => { /* 用户可手动重启 */ });
      }, 1800);
      return "installed";
    }
    return "none";
  } catch (e) {
    const msg = String((e as { message?: string })?.message ?? e ?? "").slice(0, 120);
    onStatus(`更新失败：${msg || "未知错误"}`);
    return "error";
  }
}
