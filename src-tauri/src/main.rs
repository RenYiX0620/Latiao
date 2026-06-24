#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Manager;
/// Proxy HTTP request to sidecar — bypasses Tauri HTTP plugin entirely
#[tauri::command]
async fn sidecar_proxy(url: String, method: String, body: Option<String>) -> Result<String, String> {
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
    let resp = req.send().await.map_err(|e| format!("Request failed: {}", e))?;
    let text = resp.text().await.map_err(|e| format!("Read failed: {}", e))?;
    Ok(text)
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
#[cfg(target_os = "windows")]
#[tauri::command]
fn store_secret(key: String, value: String) -> Result<(), String> {
    use std::process::Stdio;
    let status = Command::new("cmdkey")
        .args(["/add", &format!("latiao:{}", key), "/user", "latiao", "/pass", &value])
        .stdout(Stdio::null()).stderr(Stdio::null())
        .status().map_err(|e| format!("cmdkey failed: {}", e))?;
    if status.success() { Ok(()) } else { Err("cmdkey exited non-zero".into()) }
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn get_secret(_key: String) -> Result<String, String> {
    Err("Not found".into())
}

#[cfg(target_os = "windows")]
#[tauri::command]
fn delete_secret(key: String) -> Result<(), String> {
    use std::process::Stdio;
    Command::new("cmdkey")
        .args(["/delete", &format!("latiao:{}", key)])
        .stdout(Stdio::null()).stderr(Stdio::null())
        .status().map_err(|e| format!("cmdkey failed: {}", e))?;
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
        // Give sidecar a moment to flush, then force-kill
        let _ = child.kill();
        let _ = child.wait();
        println!("[Latiao] Sidecar stopped for restart");
    }
    let new_child = start_sidecar();
    if new_child.is_some() {
        println!("[Latiao] Sidecar restarted");
    } else {
        eprintln!("[Latiao] Failed to restart sidecar");
    }
    *guard = new_child;
    Ok("ok".to_string())
}

/// Managed state holding the sidecar child process handle.
/// Dropped on app exit → kills the sidecar automatically.
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

fn main() {
    eprintln!("[Latiao] App starting...");
    let sidecar = start_sidecar();

    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
.manage(SidecarProcess(Mutex::new(sidecar)))
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                window.on_navigation(move |url| {
                    let is_local = matches!(url.host_str(), Some("tauri.localhost") | Some("127.0.0.1") | Some("localhost"));
                    if !is_local && (url.scheme() == "http" || url.scheme() == "https") {
                        let _ = std::process::Command::new("open").arg(url.as_str()).spawn();
                        false
                    } else {
                        true
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![sidecar_proxy, restart_sidecar, store_secret, get_secret, delete_secret, open_model_dir])
        .run(tauri::generate_context!())
        .expect("Failed to start Latiao app");
}
