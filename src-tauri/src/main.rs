#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use tauri::Manager;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

/// Per-run sidecar auth token — generated once at startup, injected into the
/// sidecar process via the LATIAO_AUTH_TOKEN env var and exposed to the
/// frontend through the get_auth_token command. Stable across sidecar
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

/// Generate a random auth token. macOS/Linux: 32 bytes from /dev/urandom,
/// hex-encoded (64 chars). Windows/fallback: timestamp + PID (simple — the
/// threat model here is "local process can't guess it easily", not crypto).
fn generate_auth_token() -> String {
    #[cfg(not(target_os = "windows"))]
    {
        use std::io::Read;
        let mut buf = [0u8; 32];
        if std::fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut buf))
            .is_ok()
        {
            return hex_encode(&buf);
        }
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
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
    // Local auth: forward the frontend-supplied token as X-Latiao-Token so the
    // sidecar can verify it. Empty token (non-Tauri/no-auth mode) is skipped.
    if let Some(t) = token {
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
    let status = Command::new("security")
        .args([
            "add-generic-password",
            "-s", "com.latiao.desktop",
            "-a", &key,
            "-w", &value,
            "-U", // update if exists
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map_err(|e| format!("security CLI failed: {}", e))?;
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
        String::from_utf8(output.stdout)
            .map(|s| s.trim().to_string())
            .map_err(|e| format!("Invalid UTF-8: {}", e))
    } else {
        Err("Not found".into())
    }
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
fn secret_file(key: &str) -> std::path::PathBuf {
    let base = std::env::var("APPDATA").unwrap_or_else(|_| ".".into());
    let dir = std::path::Path::new(&base).join("latiao").join("secrets");
    let _ = std::fs::create_dir_all(&dir);
    dir.join(sanitize_secret_key(key))
}

#[cfg(target_os = "windows")]
fn sanitize_secret_key(key: &str) -> String {
    // 防止路径穿越：只保留安全字符
    key.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-').collect()
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn store_secret(key: String, value: String) -> Result<(), String> {
    std::fs::write(secret_file(&key), value).map_err(|e| format!("write secret failed: {}", e))
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn get_secret(key: String) -> Result<String, String> {
    std::fs::read_to_string(secret_file(&key)).map_err(|_| "Not found".into())
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn delete_secret(key: String) -> Result<(), String> {
    let f = secret_file(&key);
    if f.exists() {
        std::fs::remove_file(&f).map_err(|e| format!("delete secret failed: {}", e))?;
    }
    Ok(())
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

/// Restart the sidecar process — kills current child and spawns a new one.
/// Note: kill+wait+spawn is short-lived blocking I/O (typically <500ms).
/// Tauri commands run on a thread pool, so this won't block the UI.
#[tauri::command]
fn restart_sidecar(state: tauri::State<'_, SidecarProcess>) -> Result<String, String> {
    let mut guard = state.0.lock().map_err(|e| format!("Lock failed: {}", e))?;
    if let Some(ref mut child) = *guard {
        // 先通知 sidecar detach 模型引擎（Python 子进程），使其在 sidecar
        // 重启后继续存活（模型加载耗时巨大，重新加载会中断用户任务）
        // token 必须走 stdin 而不是 -H 参数：argv 会出现在 `ps` 输出里泄漏
        let token = AUTH_TOKEN.get().map(|s| s.as_str()).unwrap_or("").to_string();
        // sidecar 的 _check_auth 只认 x-latiao-token / Authorization 头，
        // 此前 token 只放 body → 生产模式必 401、detach 失效、引擎被杀（P1-8）
        let auth_header = format!("Authorization: Bearer {}", token);
        let mut curl = Command::new("curl")
            .args([
                "-s", "-m", "2", "-X", "POST",
                "-H", "Content-Type: application/json",
                "-H", &auth_header,
                "--data-binary", "@-",
                "http://127.0.0.1:8765/v1/engine/detach",
            ])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .ok();
        if let Some(ref mut c) = curl {
            use std::io::Write;
            if let Some(stdin) = c.stdin.as_mut() {
                let _ = writeln!(stdin, "{{\"token\":\"{}\"}}", token);
            }
        }
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
        // 同步全局退出清理句柄（RunEvent::Exit 读这里）
        *APP_SIDECAR.lock().unwrap_or_else(|e| e.into_inner()) = guard.take();
        Ok("ok".to_string())
    } else {
        eprintln!("[Latiao] Failed to restart sidecar");
        *guard = None;
        *APP_SIDECAR.lock().unwrap_or_else(|e| e.into_inner()) = None;
        Err("Sidecar 启动失败：未找到 main.py 或可用的 Python 运行时。请尝试重启应用。".to_string())
    }
}

/// Managed state holding the sidecar child process handle.
/// 注意：Tauri 的 App::run() 以 process::exit 结束、不会 Drop managed state。
/// 退出清理走 RunEvent::Exit -> kill_sidecar_on_exit()（读 APP_SIDECAR）。
struct SidecarProcess(Mutex<Option<Child>>);

static APP_SIDECAR: std::sync::Mutex<Option<Child>> = std::sync::Mutex::new(None);

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
    if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(&path).spawn().map_err(|e| e.to_string())?;
    } else if cfg!(target_os = "windows") {
        std::process::Command::new("explorer").arg(&path).spawn().map_err(|e| e.to_string())?;
    } else {
        std::process::Command::new("xdg-open").arg(&path).spawn().map_err(|e| e.to_string())?;
    }
    Ok(path)
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

    // Kill stale sidecar via PID file (precise — avoids killing unrelated processes)
    // Uses platform-specific commands: kill on macOS/Linux, taskkill on Windows
    let pid_file = home_dir().join(".local-ai-os").join("sidecar.pid");
    if let Ok(pid_str) = std::fs::read_to_string(&pid_file) {
        if let Ok(pid) = pid_str.trim().parse::<i32>() {
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

    match cmd
        .current_dir(&sidecar_dir)
        .env("LATIAO_CTX_LEN", "64000")
        .env("LATIAO_AUTH_TOKEN", AUTH_TOKEN.get().map(|s| s.as_str()).unwrap_or(""))
        .env("LATIAO_APP_VERSION", env!("CARGO_PKG_VERSION"))
        .spawn()
    {
        Ok(child) => {
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
    let sidecar = start_sidecar();
    let sidecar_ok = sidecar.is_some();
    if !sidecar_ok {
        eprintln!("[Latiao] WARNING: sidecar failed to start — AI features will be unavailable");
    }
    *APP_SIDECAR.lock().unwrap_or_else(|e| e.into_inner()) = sidecar;
    let managed_sidecar = SidecarProcess(Mutex::new(None));

    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init())
        .manage(managed_sidecar)
        .invoke_handler(tauri::generate_handler![sidecar_proxy, get_auth_token, restart_sidecar, store_secret, get_secret, delete_secret, open_model_dir])
        .on_window_event(|window, event| {
            // 关闭 = 隐藏到托盘（定时任务/sidecar 持续运行），托盘菜单可退出
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .setup(move |app| {
            setup_tray(app.handle())?;
            if !sidecar_ok {
                // 启动失败：前端恢复面板会自动检测并展示自助恢复能力
                // （健康探测 / 重启 sidecar / 导出日志），不再弹阻塞对话框。
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Failed to build Latiao app")
        .run(|_app_handle, event| {
            // App::run() 不会返回（内部 process::exit，不跑 Drop），
            // 必须在这里显式清理 sidecar 子进程
            if let tauri::RunEvent::Exit = event {
                kill_sidecar_on_exit();
            }
        });
}

/// 应用退出时的进程清理。App::run() 内部以 std::process::exit 结束进程，
/// 不运行任何 Drop —— 必须在 RunEvent::Exit 显式清理，否则 sidecar 成为
/// 孤儿：cron 定时任务继续调云端 API（持续烧钱）、模型引擎常驻内存。
fn kill_sidecar_on_exit() {
    let mut state = APP_SIDECAR.lock().unwrap_or_else(|e| e.into_inner());
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
