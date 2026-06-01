#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;

/// Managed state holding the sidecar child process handle.
/// Dropped on app exit → kills the sidecar automatically.
struct SidecarProcess(Mutex<Option<Child>>);

impl Drop for SidecarProcess {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(ref mut child) = *guard {
                let _ = child.kill();
                let _ = child.wait();
                println!("[Latiao] Sidecar stopped");
            }
        }
    }
}

fn home_dir() -> std::path::PathBuf {
    std::path::PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/tmp".into()))
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

    // Prefer venv Python, fall back to system python3
    let venv_python = sidecar_dir.join("venv").join("bin").join("python3");
    let python = if venv_python.exists() { venv_python } else { std::path::PathBuf::from("python3") };

    match Command::new(python)
        .arg("main.py")
        .current_dir(&sidecar_dir)
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
        .manage(SidecarProcess(Mutex::new(sidecar)))
        .run(tauri::generate_context!())
        .expect("Failed to start Latiao app");
}
