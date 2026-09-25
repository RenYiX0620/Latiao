#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use tauri::Manager;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

/// Per-run sidecar auth token — generated once at startup, handed to the
/// sidecar over its stdin (never the environment: `ps eww` on macOS reveals a
/// process's exec-time env, and the model itself can run commands) and exposed
/// to the frontend through the get_auth_token command. Stable across sidecar
/// restarts so the frontend's cached token stays valid.
static AUTH_TOKEN: OnceLock<String> = OnceLock::new();

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0x0f) as usize] as char);
    }
    s
}

/// Generate a random auth token: 32 bytes from the OS random source,
/// hex-encoded (64 chars).
///
/// 2026-09-23（审计 P1）：旧实现只在非 Windows 上读 /dev/urandom，Windows
/// 必然落到"时间戳+PID"——可被枚举/预测，本机鉴权可被绕过。现在统一走
/// getrandom（Windows 上内部用 BCryptGenRandom/ProcessPrng）。
fn generate_auth_token() -> String {
    let mut buf = [0u8; 32];
    if getrandom::fill(&mut buf).is_ok() {
        return hex_encode(&buf);
    }
    // 兜底（getrandom 失败极罕见）：Unix 上直接读 /dev/urandom
    #[cfg(not(target_os = "windows"))]
    {
        use std::io::Read;
        if std::fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut buf))
            .is_ok()
        {
            return hex_encode(&buf);
        }
    }
    // 最后手段：时间戳+PID。仅用于"随机源完全不可用"的退化环境，
    // **不作为安全随机**（保留是为了让应用还能启动，而不是静默降级成弱鉴权）。
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
    eprintln!("[latiao] 操作系统随机源不可用，token 退化为弱随机（请排查环境）");
    format!("{:x}-{:x}", now, std::process::id())
}

/// Proxy HTTP request to sidecar — bypasses Tauri HTTP plugin entirely.
/// Allowlist: only http://127.0.0.1:8765 (the local sidecar). Prevents the
/// webview from abusing this command as an open proxy / SSRF surface
/// (incl. `@`-userinfo host spoofing and cloud-metadata endpoints).
#[tauri::command]
async fn sidecar_proxy(
    url: String,
    method: String,
    body: Option<String>,
    token: Option<String>,
) -> Result<String, String> {
    let parsed = reqwest::Url::parse(&url)
        .map_err(|e| format!("Invalid URL: {}", e))?;
    let allowed = parsed.scheme() == "http"
        && matches!(parsed.host_str(), Some("127.0.0.1") | Some("localhost"))
        && parsed.port_or_known_default() == Some(8765);
    if !allowed {
        return Err(format!(
            "Blocked: sidecar_proxy only permits http://127.0.0.1:8765, got {}",
            url
        ));
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(120))
        // 禁止跟随 3xx：allowlist 只校验了初始 URL（P1 SSRF 逃逸）
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|e| format!("Client build failed: {}", e))?;
    let mut req = match method.as_str() {
        "GET" => client.get(&url),
        "POST" => client.post(&url),
        "DELETE" => client.delete(&url),
        _ => return Err(format!("Unsupported method: {}", method)),
    };
    if let Some(b) = body {
        req = req.header("Content-Type", "application/json").body(b);
    }
    // Local auth: always attach process-level AUTH_TOKEN (ignore frontend value).
    let _ = token;
    if let Some(t) = AUTH_TOKEN.get() {
        if !t.is_empty() {
            req = req.header("X-Latiao-Token", t);
        }
    }
    let resp = req.send().await.map_err(|e| format!("Request failed: {}", e))?;
    let text = resp.text().await.map_err(|e| format!("Read failed: {}", e))?;
    Ok(text)
}

/// Return the per-run sidecar auth token so the frontend can attach it as the
/// X-Latiao-Token header on sidecar requests.
#[tauri::command]
fn get_auth_token() -> Result<String, String> {
    AUTH_TOKEN
        .get()
        .cloned()
        .ok_or_else(|| "Auth token not initialized".to_string())
}

/// Store a secret in the macOS Keychain via the `security` CLI.
#[cfg(target_os = "macos")]
#[tauri::command]
fn store_secret(key: String, value: String) -> Result<(), String> {
    let mut child = Command::new("security")
        .args([
            "add-generic-password",
            "-s", "com.latiao.desktop",
            "-a", &key,
            // ② 审计 P1：-w 不带值 → security 从 stdin 读密码。
            // 旧写法是把值直接跟在这个开关后面 → 明文密钥进 argv，`ps` 就能看到
            //（Python 侧写 keychain 一直是这么做的，Rust 侧照抄）。
            "-U", // update if exists —— 必须在 -w 之前：-w 会把紧跟的参数当成密码值
            "-w", // 值走 stdin（两行），见下
        ])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("security CLI failed: {}", e))?;
    {
        use std::io::Write;
        // take() 而不是 as_mut()：写完这块句柄即被 drop、管道关闭 → 子进程看到 EOF。
        // 实测（2026-09-23）`security` 会一直等到 stdin EOF 才返回：用
        // `(printf 'v\nv\n'; sleep 8) | security add-generic-password … -U -w` 量到
        // 8.008s。若写端始终握在 child 手里，wait() 就永久阻塞、UI 存密钥卡死。
        if let Some(mut stdin) = child.stdin.take() {
            // security 会连问两遍（password + retype）→ stdin 必须送两行。
            // 实测：只送一行会报 "passwords don't match"；把值跟着 -w 就会进 argv。
            let payload = format!("{v}\n{v}\n", v = value);
            stdin
                .write_all(payload.as_bytes())
                .map_err(|e| format!("write secret to stdin failed: {}", e))?;
        }
    }
    let status = child
        .wait()
        .map_err(|e| format!("security CLI wait failed: {}", e))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("security exited with {}", status))
    }
}

/// Retrieve a secret from the macOS Keychain via the `security` CLI.
#[cfg(target_os = "macos")]
#[tauri::command]
fn get_secret(key: String) -> Result<String, String> {
    let output = Command::new("security")
        .args([
            "find-generic-password",
            "-s", "com.latiao.desktop",
            "-a", &key,
            "-w",
        ])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .output()
        .map_err(|e| format!("security CLI failed: {}", e))?;
    if output.status.success() {
        // 只去掉尾部换行；trim() 会吞掉密钥首尾空白
        String::from_utf8(output.stdout)
            .map(|s| s.trim_end_matches(['\n', '\r']).to_string())
            .map_err(|e| format!("Invalid UTF-8: {}", e))
    } else {
        Err("Not found".into())
    }
}

/// Whether a secret exists — without returning its value.
#[cfg(target_os = "macos")]
#[tauri::command]
fn has_secret(key: String) -> Result<bool, String> {
    let output = Command::new("security")
        .args([
            "find-generic-password",
            "-s", "com.latiao.desktop",
            "-a", &key,
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map_err(|e| format!("security CLI failed: {}", e))?;
    Ok(output.success())
}

/// Delete a secret from the macOS Keychain via the `security` CLI.
#[cfg(target_os = "macos")]
#[tauri::command]
fn delete_secret(key: String) -> Result<(), String> {
    let status = Command::new("security")
        .args([
            "delete-generic-password",
            "-s", "com.latiao.desktop",
            "-a", &key,
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map_err(|e| format!("security CLI failed: {}", e))?;
    if status.success() {
        Ok(())
    } else {
        // Not found is also OK (already deleted)
        Ok(())
    }
}

/// Cross-platform stubs: non-macOS platforms use in-memory storage for now.
/// TODO: Windows Credential Manager + Linux Secret Service integration.
/// Windows: store secrets in %APPDATA%\latiao\secrets\<key>.
/// (cmdkey 写入的凭据无法读取明文，改为文件存储保证读写一致)
#[cfg(target_os = "windows")]
fn sanitize_secret_key(key: &str) -> String {
    key.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-').collect()
}

#[cfg(target_os = "windows")]
fn win_keyring(key: &str) -> Result<keyring::Entry, String> {
    keyring::Entry::new("com.latiao.desktop", &sanitize_secret_key(key))
        .map_err(|e| format!("keyring entry failed: {}", e))
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn store_secret(key: String, value: String) -> Result<(), String> {
    win_keyring(&key)?
        .set_password(&value)
        .map_err(|e| format!("write secret failed: {}", e))
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn get_secret(key: String) -> Result<String, String> {
    win_keyring(&key)?
        .get_password()
        .map_err(|_| "Not found".into())
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn delete_secret(key: String) -> Result<(), String> {
    match win_keyring(&key)?.delete_credential() {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(format!("delete secret failed: {}", e)),
    }
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn has_secret(key: String) -> Result<bool, String> {
    match win_keyring(&key)?.get_password() {
        Ok(_) => Ok(true),
        Err(keyring::Error::NoEntry) => Ok(false),
        Err(e) => Err(format!("has_secret failed: {}", e)),
    }
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
#[tauri::command]
fn store_secret(_key: String, _value: String) -> Result<(), String> {
    Err("Secret storage not yet implemented on this platform".into())
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
#[tauri::command]
fn get_secret(_key: String) -> Result<String, String> {
    Err("Secret storage not yet implemented on this platform".into())
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
#[tauri::command]
fn delete_secret(_key: String) -> Result<(), String> {
    Err("Secret storage not yet implemented on this platform".into())
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
#[tauri::command]
fn has_secret(_key: String) -> Result<bool, String> {
    Ok(false)
}

/// 一次性迁移：旧版把全部渠道 token 塞在单个 `channel_tokens` JSON 里，
/// 新版改成 `channel_tokens:<channel>` 逐项存放（避免整包进 webview）。
/// 在 Rust 侧拆开写入后删掉旧键，明文不经前端。
#[tauri::command]
fn migrate_channel_tokens() -> Result<u32, String> {
    let raw = match get_secret("channel_tokens".to_string()) {
        Ok(v) => v,
        Err(_) => return Ok(0),
    };
    let parsed: std::collections::HashMap<String, String> = match serde_json::from_str(&raw) {
        Ok(m) => m,
        Err(_) => return Ok(0),
    };
    let mut n = 0u32;
    for (ch, val) in parsed {
        if ch.is_empty() || val.is_empty() {
            continue;
        }
        store_secret(format!("channel_tokens:{}", ch), val)?;
        n += 1;
    }
    let _ = delete_secret("channel_tokens".to_string());
    Ok(n)
}

/// Restart the sidecar process — kills current child and spawns a new one.
/// Note: kill+wait+spawn is short-lived blocking I/O (typically <500ms).
/// Tauri commands run on a thread pool, so this won't block the UI.
/// 给 sidecar 发一个本机回环 POST（detach 引擎 / 停引擎用）。
///
/// ① 审计 P1：旧实现 spawn `curl -H "Authorization: Bearer <token>"` ——
/// token 出现在 argv 里，任何本机进程 `ps` 就能拿到。注释当时写的是
/// "token 必须走 stdin 而不是 -H 参数"，代码却正好相反。现在改成**进程内
/// reqwest 请求**：不 spawn 子进程、argv 里没有 token，也不再依赖 curl。
fn post_to_sidecar(path: &str, timeout_ms: u64) -> bool {
    let token = AUTH_TOKEN.get().map(|s| s.as_str()).unwrap_or("").to_string();
    let url = format!("http://127.0.0.1:8765{}", path);
    let client = match reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_millis(timeout_ms))
        .redirect(reqwest::redirect::Policy::none())
        .build()
    {
        Ok(c) => c,
        Err(_) => return false,
    };
    let mut req = client
        .post(&url)
        .header("Content-Type", "application/json")
        .json(&serde_json::json!({ "token": token }));
    if !token.is_empty() {
        req = req.header("Authorization", format!("Bearer {}", token));
    }
    matches!(req.send(), Ok(_))
}

#[tauri::command]
fn restart_sidecar(state: tauri::State<'_, SidecarProcess>) -> Result<String, String> {
    let mut guard = state.0.lock().map_err(|e| format!("Lock failed: {}", e))?;
    if let Some(ref mut child) = *guard {
        // 先通知 sidecar detach 模型引擎（Python 子进程），使其在 sidecar
        // 重启后继续存活（模型加载耗时巨大，重新加载会中断用户任务）。
        // 走进程内 reqwest（token 不进 argv，见 post_to_sidecar）。
        let _ = post_to_sidecar("/v1/engine/detach", 2000);
        // detach 请求发出后稍候片刻再杀 sidecar（给 Python 处理时间）
        std::thread::sleep(std::time::Duration::from_millis(300));

        // Give sidecar a moment to flush, then force-kill
        let _ = child.kill();
        let _ = child.wait();
        println!("[Latiao] Sidecar stopped for restart");
    }
    let new_child = start_sidecar();
    if new_child.is_some() {
        println!("[Latiao] Sidecar restarted");
        *guard = new_child;
        Ok("ok".to_string())
    } else {
        eprintln!("[Latiao] Failed to restart sidecar");
        *guard = None;
        Err("Sidecar 启动失败：未找到 main.py 或可用的 Python 运行时。请尝试重启应用。".to_string())
    }
}

/// Managed state holding the sidecar child process handle.
/// 注意：Tauri 的 App::run() 以 process::exit 结束、不会 Drop managed state。
/// 退出清理走 RunEvent::Exit -> kill_sidecar_on_exit()（读 managed state）。
struct SidecarProcess(Mutex<Option<Child>>);

impl Drop for SidecarProcess {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                // Wait in a background thread — drop must not block the main thread
                std::thread::spawn(move || {
                    let _ = child.wait();
                    println!("[Latiao] Sidecar stopped");
                });
            }
        }
    }
}

fn home_dir() -> std::path::PathBuf {
    #[cfg(target_os = "windows")]
    {
        std::path::PathBuf::from(
            std::env::var("USERPROFILE").unwrap_or_else(|_| "C:\\".into())
        )
    }
    #[cfg(not(target_os = "windows"))]
    {
        std::path::PathBuf::from(
            std::env::var("HOME").unwrap_or_else(|_| "/tmp".into())
        )
    }
}

#[tauri::command]
fn open_model_dir() -> Result<String, String> {
    let models_dir = home_dir().join("Models");
    std::fs::create_dir_all(&models_dir).map_err(|e| e.to_string())?;
    let path = models_dir.to_string_lossy().to_string();
    open_with_system(&path)?;
    Ok(path)
}

/// 用系统默认程序打开/显示文件（Office 走这条；图/PDF/代码也提供作兜底）。
#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    if path.trim().is_empty() {
        return Err("empty path".into());
    }
    // 只允许绝对路径，拒绝穿越片段——避免把 webview 来的相对路径/命令行塞进 open
    if !std::path::Path::new(&path).is_absolute() || path.contains("..") {
        return Err("path must be absolute and without ..".into());
    }
    open_with_system(&path)
}

fn open_with_system(path: &str) -> Result<(), String> {
    if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(path).spawn().map_err(|e| e.to_string())?;
    } else if cfg!(target_os = "windows") {
        std::process::Command::new("explorer").arg(path).spawn().map_err(|e| e.to_string())?;
    } else {
        std::process::Command::new("xdg-open").arg(path).spawn().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// 残留 pid 文件里那个进程的归属（三态）。
///
/// 必须是三态：pid 文件只在 sidecar 优雅退出时才删，而所有强杀路径（重启后端、
/// 更新前停后端、Python 看门狗 os._exit）都会留下它，所以"文件在、进程已死"是常态。
/// 早期实现把"进程不存在"和"身份不符"混为一谈并直接中止启动，后果是重启后端第一次
/// 必失败、带残留 pid 文件的冷启动直接没有 sidecar（2026-09-24 复查发现）。
enum StalePidState {
    /// 进程不存在（或判定不出来）→ 清 pid 文件，继续启动
    Gone,
    /// 进程存在但不是自家 sidecar（PID 复用）→ 不杀，继续启动
    NotOurs,
    /// 自家 sidecar → 先杀再启动
    Ours,
}

fn classify_stale_pid(pid: i32) -> StalePidState {
    if pid <= 1 {
        return StalePidState::Gone;
    }
    #[cfg(unix)]
    {
        match Command::new("ps")
            .args(["-p", &pid.to_string(), "-o", "comm="])
            .output()
        {
            // 进程不存在时 ps 退出码非 0、stdout 为空 —— 这是"已死"，不是"不是自家"
            Ok(out) => {
                let comm = String::from_utf8_lossy(&out.stdout).to_lowercase();
                if comm.trim().is_empty() {
                    StalePidState::Gone
                } else if comm.contains("python")
                    || comm.contains("sidecar")
                    || comm.contains("latiao")
                {
                    StalePidState::Ours
                } else {
                    StalePidState::NotOurs
                }
            }
            Err(_) => StalePidState::Gone,
        }
    }
    #[cfg(not(unix))]
    {
        match Command::new("tasklist")
            .args(["/FI", &format!("PID eq {}", pid), "/FO", "CSV", "/NH"])
            .output()
        {
            Ok(out) => {
                let line = String::from_utf8_lossy(&out.stdout).to_lowercase();
                if line.trim().is_empty() || line.contains("no tasks") {
                    StalePidState::Gone
                } else if line.contains("python")
                    || line.contains("sidecar")
                    || line.contains("latiao")
                {
                    StalePidState::Ours
                } else {
                    StalePidState::NotOurs
                }
            }
            Err(_) => StalePidState::Gone,
        }
    }
}

fn start_sidecar() -> Option<Child> {
    // Try multiple possible locations for the sidecar directory:
    //   1. CWD/sidecar          — dev mode, CWD is project root
    //   2. CWD/../sidecar       — dev mode, CWD is src-tauri/
    //   3. EXE_DIR/sidecar      — production bundle resource
    //   4. EXE_DIR/../Resources/sidecar — macOS .app resource dir
    let cwd = std::env::current_dir().ok()?;
    let exe_dir = std::env::current_exe().ok()?.parent()?.to_path_buf();

    let candidates: Vec<std::path::PathBuf> = vec![
        cwd.join("sidecar"),
        cwd.parent().map(|p| p.join("sidecar")).unwrap_or_default(),
        exe_dir.join("sidecar"),
        exe_dir.join("..").join("Resources").join("sidecar"),
    ];

    let sidecar_dir = candidates.iter().find(|d| d.join("main.py").exists())?;

    let main_py = sidecar_dir.join("main.py");
    if !main_py.exists() {
        eprintln!("[Latiao] sidecar not found at {}", main_py.display());
        return None;
    }

    // 清残留 sidecar（PID 文件）。三态：进程已死（常态）或不是自家进程（PID 复用）
    // 都只清文件、**继续启动**；只有确认是自家 sidecar 才杀。
    let pid_file = home_dir().join(".local-ai-os").join("sidecar.pid");
    if let Ok(pid_str) = std::fs::read_to_string(&pid_file) {
        if let Ok(pid) = pid_str.trim().parse::<i32>() {
            match classify_stale_pid(pid) {
                StalePidState::Ours => {
                    let _ = std::thread::spawn(move || {
                        #[cfg(target_os = "windows")]
                        let _ = Command::new("taskkill")
                            .args(["/PID", &pid.to_string(), "/F"])
                            .stdout(std::process::Stdio::null())
                            .stderr(std::process::Stdio::null())
                            .spawn();
                        #[cfg(not(target_os = "windows"))]
                        let _ = Command::new("kill")
                            .arg(pid.to_string())
                            .stdout(std::process::Stdio::null())
                            .stderr(std::process::Stdio::null())
                            .spawn();
                    }).join();
                    std::thread::sleep(std::time::Duration::from_millis(500));
                }
                StalePidState::NotOurs => {
                    eprintln!("[Latiao] stale pid {} is not a sidecar (PID reuse); skip kill", pid);
                    let _ = std::fs::remove_file(&pid_file);
                }
                StalePidState::Gone => {
                    let _ = std::fs::remove_file(&pid_file);
                }
            }
        }
    }

    #[cfg(target_os = "windows")]
    let sidecar_exe = sidecar_dir.join("sidecar.exe");
    #[cfg(not(target_os = "windows"))]
    let bundled_python = sidecar_dir.join("python").join("bin").join("python3");
    #[cfg(not(target_os = "windows"))]
    let venv_python = sidecar_dir.join("venv").join("bin").join("python3");

    #[cfg(target_os = "windows")]
    let mut cmd = Command::new(&sidecar_exe);
    #[cfg(not(target_os = "windows"))]
    let mut cmd = {
        let python = if bundled_python.exists() { bundled_python }
                     else if venv_python.exists() { venv_python }
                     else { std::path::PathBuf::from("python3") };
        let mut c = Command::new(python);
        c.arg("main.py");
        c
    };

    let spawned = cmd
        .current_dir(&sidecar_dir)
        .env("LATIAO_CTX_LEN", "64000")
        .env("LATIAO_APP_VERSION", env!("CARGO_PKG_VERSION"))
        // (A) token 不再进环境变量：实测 `ps eww -p <sidecar pid>` 能把同用户进程
        // 启动时的整份环境打出来（运行时 os.environ.pop 也无效——ps 读的是 exec 时的
        // 快照），而模型自己就能跑命令，等于把全权 token 送到它手上。改走子进程
        // stdin 传一行（Python 侧启动时读一次）。
        .env_remove("LATIAO_AUTH_TOKEN")
        // (C) 妙想 key 同理：不再继承进 sidecar 环境，改由 sidecar 从 config.json
        // 读入内存，需要时显式注入给子进程。
        .env_remove("MX_APIKEY")
        .stdin(std::process::Stdio::piped())
        .spawn();

    match spawned {
        Ok(mut child) => {
            // 把 token 写进子进程 stdin 后立刻 drop 写端（管道 EOF）：sidecar 读一行
            // 即完成鉴权初始化；EOF 也让它知道没有更多输入。
            if let Some(mut stdin) = child.stdin.take() {
                let token = AUTH_TOKEN.get().map(|s| s.as_str()).unwrap_or("");
                use std::io::Write;
                if let Err(e) = stdin.write_all(format!("{}\n", token).as_bytes()) {
                    eprintln!("[Latiao] Failed to hand token to sidecar over stdin: {}", e);
                }
            }
            println!("[Latiao] Sidecar started (pid {})", child.id());
            Some(child)
        }
        Err(e) => {
            eprintln!("[Latiao] Failed to start sidecar: {}", e);
            None
        }
    }
}

/// 系统托盘：关闭窗口后隐藏到托盘，托盘菜单可重新显示（保持 sidecar/定时任务运行）。
fn setup_tray(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::menu::{MenuBuilder, MenuItemBuilder};
    use tauri::tray::TrayIconBuilder;
    let show = MenuItemBuilder::with_id("latiao_show", "显示辣条 Latiao").build(app)?;
    let quit = MenuItemBuilder::with_id("latiao_quit", "退出").build(app)?;
    let menu = MenuBuilder::new(app).items(&[&show, &quit]).build()?;
    TrayIconBuilder::new()
        .icon(tauri::image::Image::from_bytes(
            include_bytes!("../icons/32x32.png"),
        )?)
        .tooltip("辣条 Latiao")
        .menu(&menu)
        .on_menu_event(|app, event| {
            match event.id().as_ref() {
                "latiao_show" => {
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.show();
                        let _ = w.unminimize();
                        let _ = w.set_focus();
                    }
                }
                "latiao_quit" => {
                    app.exit(0);
                }
                _ => {}
            }
        })
        .build(app)?;
    Ok(())
}


/// 每次启动清空 WKWebView 网络缓存（不碰 LocalStorage 会话数据）。
/// index.html 及其引用的 hashed JS 不带缓存头，WKWebView 会启发式缓存，
/// 导致重新部署后界面仍是旧构建——启动时清缓存保证永远加载本次构建资源。
#[cfg(target_os = "macos")]
fn clear_webview_cache() {
    let Ok(home) = std::env::var("HOME") else { return };
    let root = std::path::Path::new(&home);
    let webkit = root.join("Library/WebKit/com.latiao.desktop/WebsiteData");
    if let Ok(entries) = std::fs::read_dir(&webkit) {
        for e in entries.flatten() {
            let p = e.path();
            if p.is_dir() {
                let _ = std::fs::remove_dir_all(p.join("NetworkCache"));
                let _ = std::fs::remove_dir_all(p.join("Cache"));
            }
        }
    }
    let caches = root.join("Library/Caches/com.latiao.desktop");
    if let Ok(entries) = std::fs::read_dir(&caches) {
        for e in entries.flatten() {
            let p = e.path();
            let name = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
            if p.is_dir() && name.contains("WebKit") {
                let _ = std::fs::remove_dir_all(&p);
            }
        }
    }
}

fn main() {
    eprintln!("[Latiao] App starting...");
    #[cfg(target_os = "macos")]
    clear_webview_cache();
    let _ = AUTH_TOKEN.set(generate_auth_token());

    tauri::Builder::default()
        // 单实例必须在**启动 sidecar 之前**生效：插件的 setup 早于应用 setup，而
        // start_sidecar 会清残留 pid 文件、必要时杀旧 sidecar —— 顺序反了就会变成
        // "第二实例先杀掉第一实例的 sidecar、再自己退出"，第一实例从此没有 sidecar
        // （2026-09-24 复查发现）。所以 sidecar 启动搬进 .setup()。
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![sidecar_proxy, get_auth_token, restart_sidecar, stop_sidecar_for_update, store_secret, get_secret, delete_secret, has_secret, migrate_channel_tokens, open_model_dir, open_path])
        .on_window_event(|window, event| {
            // 关闭 = 隐藏到托盘（定时任务/sidecar 持续运行），托盘菜单可退出
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .setup(move |app| {
            setup_tray(app.handle())?;
            // sidecar 在这里启动：单实例插件（上面注册，setup 早于本回调）此时已判定完，
            // 第二实例已退出，不会再来抢 pid 文件 / 杀第一实例的 sidecar。
            let sidecar = start_sidecar();
            if sidecar.is_none() {
                // 启动失败：前端恢复面板会自动检测并展示自助恢复能力
                // （健康探测 / 重启 sidecar / 导出日志），不再弹阻塞对话框。
                eprintln!("[Latiao] WARNING: sidecar failed to start — AI features will be unavailable");
            }
            // 单一来源：sidecar 子进程只存 managed state——此前 managed 恒为 None，
            // restart_sidecar 的 detach 分支永不执行，每次重启都 SIGTERM 旧 sidecar →
            // lifespan 关闭钩子杀模型引擎（审计 P1：重启侧车=模型白加载一轮）
            app.manage(SidecarProcess(Mutex::new(sidecar)));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Failed to build Latiao app")
        .run(|_app_handle, event| {
            // App::run() 不会返回（内部 process::exit，不跑 Drop），
            // 必须在这里显式清理 sidecar 子进程
            if let tauri::RunEvent::Exit = event {
                kill_sidecar_on_exit(_app_handle);
            }
        });
}

/// 更新安装前停止 sidecar 与本地引擎（09-13 事故：Windows 更新时报
/// "Error opening file for writing: ...\sidecar\sidecar.exe"——安装程序只强杀
/// 主程序，残留 sidecar/llama-server 锁住文件导致覆盖失败）。
/// 先请 sidecar 优雅停掉引擎，再终止其子进程；NSIS 钩子另有一层 taskkill 兜底。
#[tauri::command]
fn stop_sidecar_for_update(state: tauri::State<'_, SidecarProcess>) -> Result<String, String> {
    // 停本地引擎（Windows 上 llama-server.exe 也在安装目录里，同样会锁文件）。
    // 同样走进程内 reqwest：token 不进 argv。
    let _ = post_to_sidecar("/v1/local-llm/stop", 3000);
    let mut guard = state.0.lock().map_err(|e| format!("Lock failed: {}", e))?;
    if let Some(mut child) = guard.take() {
        #[cfg(unix)]
        {
            let pid = child.id();
            let _ = Command::new("kill").arg("-TERM").arg(pid.to_string()).output();
            for _ in 0..20 {
                if let Ok(Some(_)) = child.try_wait() { return Ok("stopped".into()); }
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    Ok("stopped".into())
}

/// 应用退出时的进程清理。App::run() 内部以 std::process::exit 结束进程，
/// 不运行任何 Drop —— 必须在 RunEvent::Exit 显式清理，否则 sidecar 成为
/// 孤儿：cron 定时任务继续调云端 API（持续烧钱）、模型引擎常驻内存。
fn kill_sidecar_on_exit(app_handle: &tauri::AppHandle) {
    let state = app_handle.state::<SidecarProcess>();
    let mut state = match state.0.lock() {
        Ok(g) => g,
        Err(e) => e.into_inner(),
    };
    if let Some(mut child) = state.take() {
        eprintln!("[Latiao] App exit: stopping sidecar pid={:?}", child.id());
        // 先优雅终止（sidecar 的 lifespan 会正常关闭），超时再强杀
        #[cfg(unix)]
        {
            use std::process::Command;
            let pid = child.id();
            let _ = Command::new("kill").arg("-TERM").arg(pid.to_string()).output();
            for _ in 0..30 {
                if let Ok(Some(_)) = child.try_wait() { return; }
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
        }
        let _ = child.kill();
        std::thread::spawn(move || { let _ = child.wait(); });
    }
}
