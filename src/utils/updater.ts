/**
 * 应用自动更新（审计修复：此前更新功能前端零接线——开关是死开关、
 * 版本号硬编码，任何设备都不会发生更新检查）。
 */
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import { getVersion } from "@tauri-apps/api/app";

export async function getAppVersion(): Promise<string> {
  try {
    return await getVersion();
  } catch {
    return "0.3.1";
  }
}

export type UpdateCheckResult = "none" | "error" | "installed";

/**
 * 检查更新并下载安装。onStatus 用于 UI 提示（toast）。
 * - 无更新 → "none"
 * - 有更新且安装完成 → 自动 relaunch，"installed"
 * - 异常 → "error"
 */
export async function checkForUpdates(onStatus: (msg: string) => void): Promise<UpdateCheckResult> {
  try {
    const update = await check();
    if (!update) return "none";
    onStatus(`发现新版本 ${update.version}，正在下载…`);
    let finished = false;
    let chunks = 0;
    await update.downloadAndInstall((ev) => {
      if (ev.event === "Started") {
        onStatus(`开始下载 ${update.version}…`);
      } else if (ev.event === "Progress") {
        chunks += 1;
        if (chunks % 60 === 0) onStatus("下载中…");
      } else if (ev.event === "Finished") {
        finished = true;
        onStatus("下载完成，正在安装…");
      }
    });
    if (finished) {
      onStatus("更新已安装，即将重启应用…");
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
