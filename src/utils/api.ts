/**
 * Shared sidecar API client — deduplicated from App.tsx, useSkills.ts, etc.
 * All requests route through Rust IPC proxy to bypass Tauri HTTP plugin CSP restrictions.
 */

const SIDECAR = "http://127.0.0.1:8000";

/** Sidecar JSON response — always has a status field, plus arbitrary payload.
 *  Return type of sidecarFetch. Use type assertions for known response shapes. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type SidecarData = Record<string, any>;

/**
 * Call the sidecar via Rust IPC proxy (invoke). Returns parsed JSON.
 */
export async function sidecarFetch(path: string, method: "GET" | "POST" | "DELETE" = "GET", body?: unknown): Promise<SidecarData> {
  const { invoke } = await import("@tauri-apps/api/core");
  const raw = await invoke("sidecar_proxy", {
    url: SIDECAR + path,
    method,
    body: body ? JSON.stringify(body) : null,
  }) as string;
  return JSON.parse(raw) as SidecarData;
}

/**
 * Poll /health until sidecar responds (up to maxRetries × delayMs).
 */
export async function waitForSidecar(maxRetries = 15, delayMs = 1000): Promise<boolean> {
  for (let i = 0; i < maxRetries; i++) {
    try {
      const resp = await fetch(SIDECAR + "/health", { signal: AbortSignal.timeout(2000) });
      if (resp.ok) return true;
    } catch { /* retry */ }
    await new Promise(r => setTimeout(r, delayMs));
  }
  return false;
}

/**
 * sidecarFetch with health-check retry loop.
 */
export async function sidecarFetchWithRetry(
  path: string, method: "GET" | "POST" | "DELETE" = "GET", body?: unknown, maxRetries = 5,
): Promise<SidecarData> {
  const healthy = await waitForSidecar();
  if (!healthy) throw new Error("Sidecar not reachable");

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      if (attempt > 0) await new Promise(r => setTimeout(r, 2000));
      return await sidecarFetch(path, method, body);
    } catch (e) {
      if (attempt === maxRetries - 1) throw e;
    }
  }
  throw new Error("sidecarFetchWithRetry: unreachable");
}
