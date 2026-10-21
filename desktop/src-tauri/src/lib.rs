mod browser;
mod imgsearch;
mod plugin_frame;

use std::net::TcpStream;
use std::os::windows::ffi::OsStrExt;
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use tauri::Manager;
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

// ---------- WebView2 browser arguments ----------

// Every webview shares one WebView2 environment (one user-data folder), and
// WebView2 refuses a second environment created with other options, so every
// webview must pass this identical string. The first part is what wry passes
// when no arguments are given (wry 0.57, src/webview2/mod.rs: its
// --disable-features default, plus --autoplay-policy=no-user-gesture-required
// because WebViewAttributes.autoplay is true by default). The rest keeps a
// hidden webview running at full speed: the Playground's browser keeps
// capturing audio while it is hidden (browser.rs hides it when the Playground
// is left), and Chromium would otherwise background that renderer and throttle
// its timers, starving the capture script.
pub const WEBVIEW_ARGS: &str = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection \
    --autoplay-policy=no-user-gesture-required \
    --disable-renderer-backgrounding --disable-background-timer-throttling \
    --disable-backgrounding-occluded-windows";

// ---------- launch configuration (backend address + API token) ----------

// The backend's address and the per-launch API token. The token is handed to
// the sidecar (env VEXT_TOKEN), which requires it on every non-GET /api/*
// request (header X-VEXT-Token) and on the browser-audio WebSocket (?token=).
// Only VEXT's own pages get it: the main webview and the plugin frame, through
// an initialization script that sets window.__VEXT__ (see init_script). The
// Playground's browser webview (remote sites) never does; its capture script
// only gets the audio WebSocket URL.
pub struct VextConfig {
    pub port: u16,
    pub token: String,
}

static CONFIG: OnceLock<VextConfig> = OnceLock::new();

pub fn config() -> &'static VextConfig {
    CONFIG.get_or_init(|| {
        // VEXT_BACKEND_PORT: development, to run beside an installed VEXT.
        let port = std::env::var("VEXT_BACKEND_PORT")
            .ok()
            .and_then(|p| p.trim().parse().ok())
            .unwrap_or(8000);
        // VEXT_TOKEN from the environment: development with VEXT_DEV_BACKEND=1,
        // where the backend was started by hand with the same VEXT_TOKEN.
        let token = std::env::var("VEXT_TOKEN")
            .ok()
            .filter(|t| !t.trim().is_empty())
            .unwrap_or_else(random_token);
        VextConfig { port, token }
    })
}

impl VextConfig {
    pub fn api_base(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    // `window.__VEXT__ = { apiBase, token }`, run before any page script.
    pub fn init_script(&self) -> String {
        let value = serde_json::json!({ "apiBase": self.api_base(), "token": self.token });
        format!("window.__VEXT__ = {value};")
    }
}

fn random_token() -> String {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).expect("the system random generator failed");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn dev_backend() -> bool {
    std::env::var("VEXT_DEV_BACKEND").map(|v| v == "1").unwrap_or(false)
}

// ---------- backend sidecar ----------

// Where the backend keeps data/, DATABASE.md and its logs. Resolved at startup:
// VEXT_ROOT if the environment sets it (handy in development, to keep
// working against a repo-local data/ folder), otherwise the per-user app data
// folder (%APPDATA%\com.vext.app), which needs no admin rights on any PC.
fn backend_data_dir(app: &tauri::App) -> std::path::PathBuf {
    if let Ok(dir) = std::env::var("VEXT_ROOT") {
        if !dir.is_empty() {
            return std::path::PathBuf::from(dir);
        }
    }
    app.path()
        .app_data_dir()
        .expect("could not resolve the per-user app data folder")
}

// PyInstaller's --onefile bootloader on Windows spawns a second, separate
// process to actually run the app and then exits itself — so the PID Tauri's
// CommandChild tracks is only the bootloader, not the real backend. Killing
// the tracked PID alone leaves the real backend orphaned, still bound to
// port 8000. Confirmed by hand: after closing the app, a `vext-
// backend.exe` process was still running and still LISTENING on 8000 — which
// is also why "run as Administrator" appeared to do nothing: the frontend
// was just talking to that stale non-elevated leftover the whole time.
// Tauri's sidecar resolution always copies/invokes the binary under its
// plain (un-suffixed) name at runtime — confirmed empirically in both dev
// and release builds, the running image is always "vext-
// backend.exe", never the target-triple-suffixed source filename in
// binaries/. `taskkill /IM` does NOT support `*` wildcards despite some
// docs implying otherwise (confirmed by hand: a wildcarded /IM matched
// nothing while the exact name worked), so this must be the exact name.
fn kill_stray_backends() {
    // VEXT_BACKEND_PORT set: a development build running beside an installed
    // VEXT, whose backend has the same image name and must be left alone.
    if std::env::var_os("VEXT_BACKEND_PORT").is_some() {
        return;
    }
    let mut taskkill = std::process::Command::new("taskkill");
    taskkill.args(["/F", "/IM", "vext-backend.exe"]);
    // taskkill is a console program: started from this windowed app without
    // CREATE_NO_WINDOW it opens a console window, the "cmd" that flashed on
    // every launch and close. (The sidecar itself already starts without one:
    // tauri-plugin-shell sets the same flag.)
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        taskkill.creation_flags(CREATE_NO_WINDOW);
    }
    let _ = taskkill.output();
}

fn spawn_backend(app: &tauri::App) {
    // Clear out any backend leaked by a previous run before spawning a fresh
    // one — otherwise a stale (possibly non-elevated) process can still be
    // squatting on the port, and this launch's sidecar would silently fail to
    // bind while the frontend keeps talking to the old one. See
    // kill_stray_backends() above.
    kill_stray_backends();

    // Spawn the backend sidecar (PyInstaller-bundled FastAPI app — see
    // sidecar_main.py) as a child process. It inherits this process's
    // elevation level automatically: launching the whole app via "Run as
    // Administrator" (or after relaunch_elevated below) gives the backend
    // elevated file access too, without any Docker involvement — see AGENTS.md.
    let data_dir = backend_data_dir(app);
    std::fs::create_dir_all(&data_dir).expect("could not create the VEXT data folder");
    let cfg = config();
    let sidecar = app
        .shell()
        .sidecar("vext-backend")
        .expect("failed to create sidecar command")
        .env("VEXT_ROOT", &data_dir)
        .env("VEXT_TOKEN", &cfg.token)
        .env("VEXT_BACKEND_PORT", cfg.port.to_string())
        // The version tauri.conf.json gives the app, for the sidebar and About (app/config.py).
        .env("VEXT_VERSION", app.package_info().version.to_string());
    let (mut rx, child) = sidecar.spawn().expect("failed to spawn backend sidecar");

    // Keep the child handle alive for the app's lifetime so it isn't dropped
    // (which would kill the process) and so it *is* killed when the app
    // exits, rather than leaking an orphaned backend.
    let child_holder: std::sync::Mutex<Option<CommandChild>> = std::sync::Mutex::new(Some(child));
    app.manage(child_holder);

    tauri::async_runtime::spawn(async move {
        use tauri_plugin_shell::process::CommandEvent;
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => print!("[backend] {}", String::from_utf8_lossy(&line)),
                CommandEvent::Stderr(line) => eprint!("[backend] {}", String::from_utf8_lossy(&line)),
                _ => {}
            }
        }
    });
}

// Blocks (this runs in `setup`, before the event loop starts, so blocking is
// exactly what we want here) until something is listening on the backend's
// port, or `timeout` elapses. A plain TCP connect is enough: uvicorn doesn't
// open the socket until its own startup has finished, so "accepts a
// connection" is a reliable proxy for "ready to serve /api/*" without pulling
// in an HTTP client crate just for this one check.
//
// This is the actual fix for the "app opens, backend isn't up yet, every
// panel fails with a raw 'Failed to fetch'" complaint: instead of racing the
// webview's first page load against the sidecar's cold start (PyInstaller
// --onefile extracts itself on every launch, so this can take a few real
// seconds), the main window is never built at all until the backend answers
// — see the call site in `setup()` below.
fn wait_for_backend_ready(port: u16, timeout: Duration) -> bool {
    let addr = format!("127.0.0.1:{port}");
    let deadline = Instant::now() + timeout;
    loop {
        if TcpStream::connect(&addr).is_ok() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
}

fn stop_backend(app: &tauri::AppHandle) {
    if dev_backend() {
        return; // not ours: started by hand
    }
    if let Some(holder) = app.try_state::<std::sync::Mutex<Option<CommandChild>>>() {
        if let Some(child) = holder.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
    // Backstop for the bootloader/real-process split described above — the
    // tracked kill() above only reaches the bootloader PID, which may have
    // already exited on its own.
    kill_stray_backends();
}

// ---------- commands ----------

// Relaunches the current exe elevated (UAC "runas") and exits this instance.
// Called from the frontend when a file operation fails with a Windows
// permission error and the owner explicitly chooses to retry elevated — never
// automatic (see AGENTS.md: "'Permission denied' on delete under Common
// Files — intentionally not 'fixed'"). Elevation here is the owner's own
// deliberate per-session choice, the same thing "run as Administrator"
// already lets them do by hand; this just saves them closing and
// right-clicking the exe themselves.
#[tauri::command]
fn relaunch_elevated(app: tauri::AppHandle) -> Result<(), String> {
    use windows::core::PCWSTR;
    use windows::Win32::UI::Shell::ShellExecuteW;
    use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let exe_wide: Vec<u16> = exe.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    let verb_wide: Vec<u16> = "runas\0".encode_utf16().collect();

    let result = unsafe {
        ShellExecuteW(
            None,
            PCWSTR(verb_wide.as_ptr()),
            PCWSTR(exe_wide.as_ptr()),
            None,
            None,
            SW_SHOWNORMAL,
        )
    };
    // ShellExecuteW returns a value > 32 on success (HINSTANCE-shaped return
    // code, a historical Win32 wart) — anything <= 32 is a real failure (e.g.
    // the user clicked "No" on the UAC prompt).
    if (result.0 as isize) <= 32 {
        return Err("Elevation was cancelled or failed.".into());
    }
    app.exit(0);
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default();
    // One VEXT at a time (owner, 2026-10-15): a second launch only brings the
    // open window forward and exits. Two used to fight over the backend (each
    // start clears every vext-backend.exe, see kill_stray_backends). Registered
    // first, as the plugin requires. Development shells (VEXT_DEV_BACKEND /
    // VEXT_BACKEND_PORT) run beside an installed VEXT, so they skip it.
    if !dev_backend() && std::env::var_os("VEXT_BACKEND_PORT").is_none() {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }));
    }
    builder
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(browser::Browser::default())
        .manage(imgsearch::ImgSearch::default())
        .manage(plugin_frame::Frames::default())
        .setup(|app| {
            // Development: VEXT_DEV_BACKEND=1 means a backend is already
            // running (python -m uvicorn app.web:app --port <VEXT_BACKEND_PORT>,
            // with the same VEXT_TOKEN), so no sidecar is started.
            if !dev_backend() {
                spawn_backend(app);
            }

            // Gate the window on the backend actually answering, instead of
            // building it immediately and letting the webview race the
            // sidecar's cold start — previously the window appeared right
            // away and every panel's first load could hit a raw "Failed to
            // fetch" if the backend (or, in VEXT_DEV_BACKEND dev mode, the
            // hand-started uvicorn) wasn't listening yet. No window at all
            // beats one that's already showing errors.
            if !wait_for_backend_ready(config().port, Duration::from_secs(25)) {
                if !dev_backend() {
                    stop_backend(app.handle());
                }
                app.dialog()
                    .message(
                        "VEXT's backend did not start within 25 seconds.\n\n\
                         This can happen if antivirus/firewall software is blocking \
                         vext-backend.exe, or if the port it needs (127.0.0.1:8000 by \
                         default) is already in use by something else.",
                    )
                    .title("VEXT — backend failed to start")
                    .kind(MessageDialogKind::Error)
                    .blocking_show();
                std::process::exit(1);
            }

            // The main window is declared in tauri.conf.json with
            // "create": false and built here, so it can get the
            // window.__VEXT__ initialization script.
            let main_config = app
                .config()
                .app
                .windows
                .iter()
                .find(|w| w.label == "main")
                .cloned()
                .expect("tauri.conf.json declares no main window");
            let main = tauri::WebviewWindowBuilder::from_config(app.handle(), &main_config)?
                .initialization_script(config().init_script())
                .additional_browser_args(WEBVIEW_ARGS)
                .build()?;
            plugin_frame::remember_main(&main);
            Ok(())
        })
        .on_window_event(|window, event| {
            plugin_frame::on_window_event(window, event);
            if window.label() == "main" {
                if let tauri::WindowEvent::Destroyed = event {
                    stop_backend(window.app_handle());
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            relaunch_elevated,
            browser::browser_place,
            browser::browser_go,
            browser::browser_state,
            imgsearch::imgsearch_place,
            imgsearch::imgsearch_go,
            imgsearch::imgsearch_close,
            imgsearch::imgsearch_pick,
            plugin_frame::plugin_frame_open,
            plugin_frame::plugin_frame_close,
            plugin_frame::plugin_frame_move,
            plugin_frame::plugin_frame_toggle,
            plugin_frame::plugin_frame_info,
            plugin_frame::plugin_frame_bypass,
            plugin_frame::plugin_frame_scroll,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
