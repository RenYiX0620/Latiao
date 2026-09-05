/**
 * Shared sidecar API client — deduplicated from App.tsx, useSkills.ts, etc.
 * All requests route through Rust IPC proxy to bypass Tauri HTTP plugin CSP restrictions.
 */

const SIDECAR = "http://127.0.0.1:8765";

/** Sidecar JSON response — always has a status field, plus arbitrary payload.
 *  Return type of sidecarFetch. Use type assertions for known response shapes. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type SidecarData = Record<string, any>;

let _token: string | null = null;

/**
 * Fetch the per-run sidecar auth token from Rust (generated once at startup,
 * injected into the sidecar via LATIAO_AUTH_TOKEN). Cached after first call.
 * Falls back to "" outside Tauri / when the command is unavailable — the
 * sidecar only enforces auth when it was started with a token injected.
 */
export async function getToken(): Promise<string> {
  if (_token !== null) return _token;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    _token = (await invoke("get_auth_token")) as string;
  } catch {
    _token = "";
  }
  return _token;
}

/**
 * Call the sidecar via Rust IPC proxy (invoke). Returns parsed JSON.
 * Times out after 30s so a hung Rust/sidecar request can't wedge the UI.
 */
export async function sidecarFetch(path: string, method: "GET" | "POST" | "DELETE" = "GET", body?: unknown): Promise<SidecarData> {
  const { invoke } = await import("@tauri-apps/api/core");
  const token = await getToken();
  const request = invoke("sidecar_proxy", {
    url: SIDECAR + path,
    method,
    body: body ? JSON.stringify(body) : null,
    token,
  }) as Promise<string>;
  const timeout = new Promise<never>((_, reject) => setTimeout(() => reject(new Error("sidecar_proxy timeout (30s)")), 30_000));
  const raw = await Promise.race([request, timeout]);
  return JSON.parse(raw) as SidecarData;
}

/**
 * fetch() + sidecar local-auth token header (X-Latiao-Token).
 * /health needs no token; every /v1/* call should go through this.
 * Uses the Tauri HTTP plugin fetch (works inside webview CSP).
 */
export async function authFetch(path: string, init?: RequestInit): Promise<Response> {
  const { fetch: pluginFetch } = await import("@tauri-apps/plugin-http");
  const headers = new Headers(init?.headers);
  const token = await getToken();
  if (token) headers.set("X-Latiao-Token", token);
  return pluginFetch(SIDECAR + path, { ...init, headers });
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

/**
 * 上传文件到 sidecar（/v1/upload_file：图片转 base64、PDF 提取文字、
 * 文本按偏好英化）。普通 fetch + 手动 token（plugin-http 不保证支持
 * FormData，CSP 已允许 connect-src 127.0.0.1:8765）。
 */
export async function uploadSidecarFile(file: File): Promise<Record<string, unknown>> {
  const token = await getToken();
  const fd = new FormData();
  fd.append("file", file);
  const resp = await fetch(SIDECAR + "/v1/upload_file", {
    method: "POST",
    body: fd,
    headers: token ? { "X-Latiao-Token": token } : {},
  });
  return resp.json();
}
