// The Playground's web browser: a second WebView2 placed inside the main
// window (Tauri multi-webview, `unstable` feature). The frontend owns the
// layout and the chrome (address bar, buttons): it reserves a rectangle and
// calls browser_place with it whenever it changes, and drives navigation with
// browser_go. This file owns the webview itself and reports its state.
//
// Commands that create or move the child webview MUST be async: a sync command
// runs on the UI thread, and add_child deadlocks waiting for that same thread.
//
// The browser shows remote sites, so it gets no IPC at all: the capabilities
// name the webviews "main" and "plugin-frame" only (never the window "main",
// which would include this child webview), and every app command needs a
// permission (build.rs AppManifest). It also can't reach VEXT's own pages:
// see allowed_navigation. A page's full screen fills the browser's rectangle,
// never the window: see contain_fullscreen.
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex};

use serde::Serialize;
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Url, WebviewBuilder, WebviewUrl};
use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2;
use webview2_com::{
    take_pwstr, ContainsFullScreenElementChangedEventHandler, DocumentTitleChangedEventHandler,
    FocusChangedEventHandler, HistoryChangedEventHandler, NavigationCompletedEventHandler,
    NavigationStartingEventHandler, SourceChangedEventHandler,
};
use windows::core::{BOOL, PWSTR};

pub const LABEL: &str = "browser";
const HOME: &str = "https://www.youtube.com/";
const SEARCH: &str = "https://duckduckgo.com/";
const CAPTURE_JS: &str = include_str!("browser_capture.js");
const STATE_EVENT: &str = "browser://state";

#[derive(Clone, Default, PartialEq, Serialize)]
pub struct BrowserState {
    url: String,
    title: String,
    can_go_back: bool,
    can_go_forward: bool,
    loading: bool,
}

#[derive(Default)]
pub struct Browser {
    state: Mutex<BrowserState>,
    // Serializes browser_place: the first calls create the webview, and two
    // concurrent creations would collide on the label; later calls then apply
    // in the order the page sent them.
    place: tauri::async_runtime::Mutex<()>,
    // The browser has the keyboard (its controller's GotFocus / LostFocus).
    // Hidden, it would keep it: Space and every other key went to the page
    // nobody sees (a YouTube video paused and played behind the Analyzer)
    // instead of VEXT's own page. See browser_place.
    focused: AtomicBool,
}

fn audio_url() -> String {
    let cfg = crate::config();
    format!("ws://127.0.0.1:{}/api/browser/audio?token={}", cfg.port, cfg.token)
}

// Top-level navigations the browser may make: web pages only. Not VEXT's own
// pages (http://tauri.localhost, and every other custom protocol, which
// WebView2 serves as http://<name>.localhost): those are "local" to Tauri's
// IPC checks. Not file: or other schemes either (a mailto: or ms-settings:
// link would hand off to another app).
fn allowed_navigation(url: &Url) -> bool {
    match url.scheme() {
        "http" | "https" => !url.host_str().is_some_and(|h| h.to_ascii_lowercase().ends_with(".localhost")),
        "about" => true,
        _ => false,
    }
}

// What the address bar typed: a web address, a bare host ("youtube.com/x",
// "localhost:3000"), or else a DuckDuckGo search.
fn address(input: &str) -> Result<Url, String> {
    let text = input.trim();
    if text.is_empty() {
        return Err("no address".into());
    }
    if text.contains("://") {
        let url = Url::parse(text).map_err(|_| "not a valid address".to_string())?;
        return if allowed_navigation(&url) && url.scheme() != "about" {
            Ok(url)
        } else {
            Err("only web addresses (http, https) can be opened here".into())
        };
    }
    let first = text.split(['/', '?', '#']).next().unwrap_or("");
    let host = first.rsplit_once(':').map_or(first, |(h, _)| h).to_ascii_lowercase();
    if !text.contains(char::is_whitespace) && (host.contains('.') || host == "localhost") {
        let local = host == "localhost" || host == "127.0.0.1";
        let scheme = if local { "http" } else { "https" };
        if let Ok(url) = Url::parse(&format!("{scheme}://{text}")) {
            if url.host_str().is_some() && allowed_navigation(&url) {
                return Ok(url);
            }
        }
    }
    Url::parse_with_params(SEARCH, &[("q", text)]).map_err(|e| e.to_string())
}

fn read_native(core: &ICoreWebView2, s: &mut BrowserState) {
    unsafe {
        let mut text = PWSTR::null();
        if core.Source(&mut text).is_ok() {
            s.url = take_pwstr(text);
        }
        let mut text = PWSTR::null();
        if core.DocumentTitle(&mut text).is_ok() {
            s.title = take_pwstr(text);
        }
        let mut flag = BOOL::default();
        if core.CanGoBack(&mut flag).is_ok() {
            s.can_go_back = flag.as_bool();
        }
        let mut flag = BOOL::default();
        if core.CanGoForward(&mut flag).is_ok() {
            s.can_go_forward = flag.as_bool();
        }
    }
}

// Refreshes the cached state from the webview (UI thread) and, if anything
// changed, tells the main page.
fn update(app: &AppHandle, core: Option<ICoreWebView2>, loading: Option<bool>) {
    let snapshot = {
        let browser = app.state::<Browser>();
        let mut s = browser.state.lock().unwrap();
        let before = s.clone();
        if let Some(core) = &core {
            read_native(core, &mut s);
        }
        if let Some(loading) = loading {
            s.loading = loading;
        }
        if *s == before {
            return;
        }
        s.clone()
    };
    let _ = app.emit_to("main", STATE_EVENT, snapshot);
}

// Native WebView2 events, which (unlike Tauri's hooks) also cover same-document
// navigations such as YouTube's pushState page changes.
fn watch(app: AppHandle, webview: &tauri::Webview, fence: Arc<AtomicI64>) -> tauri::Result<()> {
    webview.with_webview(move |platform| unsafe {
        let controller = platform.controller();
        let mut token = 0i64;
        for got in [true, false] {
            let a = app.clone();
            let handler = FocusChangedEventHandler::create(Box::new(move |_, _| {
                a.state::<Browser>().focused.store(got, Ordering::SeqCst);
                Ok(())
            }));
            let _ = if got {
                controller.add_GotFocus(&handler, &mut token)
            } else {
                controller.add_LostFocus(&handler, &mut token)
            };
        }
        let Ok(core) = controller.CoreWebView2() else { return };
        let a = app.clone();
        let _ = core.add_NavigationStarting(
            &NavigationStartingEventHandler::create(Box::new(move |wv, _| {
                update(&a, wv, Some(true));
                Ok(())
            })),
            &mut token,
        );
        let a = app.clone();
        let _ = core.add_NavigationCompleted(
            &NavigationCompletedEventHandler::create(Box::new(move |wv, _| {
                update(&a, wv, Some(false));
                Ok(())
            })),
            &mut token,
        );
        let a = app.clone();
        let _ = core.add_SourceChanged(
            &SourceChangedEventHandler::create(Box::new(move |wv, _| {
                update(&a, wv, None);
                Ok(())
            })),
            &mut token,
        );
        let a = app.clone();
        let _ = core.add_HistoryChanged(
            &HistoryChangedEventHandler::create(Box::new(move |wv, _| {
                update(&a, wv, None);
                Ok(())
            })),
            &mut token,
        );
        let a = app.clone();
        let _ = core.add_DocumentTitleChanged(
            &DocumentTitleChangedEventHandler::create(Box::new(move |wv, _| {
                update(&a, wv, None);
                Ok(())
            })),
            &mut token,
        );
        contain_fullscreen(&app, &core, fence.load(Ordering::SeqCst));
    })
}

// A page's full screen (YouTube's button or `f`, a <video>'s, any
// element.requestFullscreen()) stays inside the browser's rectangle.
//
// Tauri gives every webview it creates, child webviews included, a
// ContainsFullScreenElementChanged handler (tauri-runtime-wry, create_webview)
// that turns the whole window full screen and back: a YouTube video made VEXT's
// window full screen. WebView2 itself only lays the element over the webview's
// own bounds (resizing to the screen is left to the host), which is what this
// browser wants: the element fills the Playground's browser panel, through the
// real Fullscreen API (fullscreenchange, :fullscreen, document.fullscreenElement,
// Esc and the page's own exit button), so pages adapt as usual. So Tauri's
// handler is removed from this webview only; the main page and the plugin frame
// keep theirs.
//
// WebView2 doesn't hand out the token Tauri got. What it does (measured, runtime
// 155): the tokens of one event are numbered in registration order across
// every webview of the process, and a remove_ with a token reaches the handler
// whichever webview it was added to. So "every token below ours" would also
// remove the main page's handler. Instead a fence is taken just before the
// browser is created (take_fence: a no-op handler added to the main webview and
// removed at once). Tauri's handler for the browser is registered after it and
// ours after that, so the fence..ours range holds Tauri's handler for this
// webview (and, at most, one of a webview created in that same instant, whose
// full screen would then just stay in its own bounds too). Removing a token
// that isn't there does nothing.
fn contain_fullscreen(app: &AppHandle, core: &ICoreWebView2, fence: i64) {
    let a = app.clone();
    let handler = ContainsFullScreenElementChangedEventHandler::create(Box::new(move |_, _| {
        keep_window(&a);
        Ok(())
    }));
    let mut own = 0i64;
    unsafe {
        if core.add_ContainsFullScreenElementChanged(&handler, &mut own).is_err() {
            return;
        }
        // Without a usable fence: the registration just before ours, which
        // is Tauri's (wry adds none). keep_window covers a miss.
        let from = if (0..own).contains(&fence) && own - fence <= 64 { fence } else { own - 1 };
        for token in from.max(0)..own {
            let _ = core.remove_ContainsFullScreenElementChanged(token);
        }
    }
}

// Stores the newest ContainsFullScreenElementChanged token in `fence`, on the
// UI thread, ahead of the browser's creation (both are queued there in order).
fn take_fence(app: &AppHandle, fence: Arc<AtomicI64>) {
    let Some(main) = app.get_webview("main") else { return };
    let _ = main.with_webview(move |platform| unsafe {
        let Ok(core) = platform.controller().CoreWebView2() else { return };
        let probe = ContainsFullScreenElementChangedEventHandler::create(Box::new(|_, _| Ok(())));
        let mut token = 0i64;
        if core.add_ContainsFullScreenElementChanged(&probe, &mut token).is_ok() {
            let _ = core.remove_ContainsFullScreenElementChanged(token);
            fence.store(token, Ordering::SeqCst);
        }
    });
}

// Safety net, should a later WebView2 or Tauri get past contain_fullscreen:
// put the window back the way it was (a flash rather than staying full
// screen). Runs on the UI thread, in the same event as Tauri's handler, which
// only queues its change: two queued hops later that change has been applied,
// whichever handler ran first. Nothing to do otherwise (tao ignores a
// set_fullscreen to the state the window is already in).
fn keep_window(app: &AppHandle) {
    let Some(window) = app.get_window("main") else { return };
    let Ok(was) = window.is_fullscreen() else { return };
    queued(app.clone(), move |a| {
        queued(a, move |b| {
            let Some(window) = b.get_window("main") else { return };
            if window.is_fullscreen().is_ok_and(|now| now != was) {
                eprintln!("[browser] a page's full screen reached the window; put back");
                let _ = window.set_fullscreen(was);
            }
        })
    });
}

// Runs f on the UI thread after what the event loop already has queued.
// (run_on_main_thread called on the UI thread would run it right away.)
fn queued(app: AppHandle, f: impl FnOnce(AppHandle) + Send + 'static) {
    tauri::async_runtime::spawn(async move {
        let a = app.clone();
        let _ = app.run_on_main_thread(move || f(a));
    });
}

fn create(app: &AppHandle, x: f64, y: f64, width: f64, height: f64) -> Result<(), String> {
    let window = app.get_window("main").ok_or("main window not found")?;
    let nav = app.clone();
    // The audio capture script, with the backend's WebSocket address (and the
    // token it requires) baked in. All frames: players often live in iframes.
    let capture = CAPTURE_JS.replace("__VEXT_AUDIO_URL__", &audio_url());
    let builder = WebviewBuilder::new(LABEL, WebviewUrl::External(HOME.parse().unwrap()))
        .initialization_script_for_all_frames(capture)
        // Identical to every other webview's (see crate::WEBVIEW_ARGS): keeps the hidden page running.
        .additional_browser_args(crate::WEBVIEW_ARGS)
        .on_navigation(allowed_navigation)
        // Links that would open a new window open here instead: one browser,
        // one page. Deferred, so the navigation doesn't start inside
        // WebView2's NewWindowRequested callback.
        .on_new_window(move |url, _features| {
            if allowed_navigation(&url) {
                let nav = nav.clone();
                tauri::async_runtime::spawn(async move {
                    if let Some(wv) = nav.get_webview(LABEL) {
                        let _ = wv.navigate(url);
                    }
                });
            }
            NewWindowResponse::Deny
        })
        // No downloads: this browser is for listening, and a site shouldn't
        // be able to drop files on the PC.
        .on_download(|_, event| !matches!(event, DownloadEvent::Requested { .. }));
    // Profile: the default WebView2 user data folder the main window uses too
    // (%LOCALAPPDATA%\com.vext.app\EBWebView), which persists, so logins
    // survive restarts.
    let fence = Arc::new(AtomicI64::new(-1));
    take_fence(app, fence.clone());
    let webview = window
        .add_child(builder, LogicalPosition::new(x, y), LogicalSize::new(width, height))
        .map_err(|e| e.to_string())?;
    watch(app.clone(), &webview, fence).map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn browser_place(
    app: AppHandle,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    visible: bool,
) -> Result<(), String> {
    let browser = app.state::<Browser>();
    let _serial = browser.place.lock().await;
    let (width, height) = (width.max(1.0), height.max(1.0));
    let Some(wv) = app.get_webview(LABEL) else {
        // Created the first time it's shown; hiding a browser that doesn't
        // exist yet is nothing to do.
        return if visible { create(&app, x, y, width, height) } else { Ok(()) };
    };
    if visible {
        wv.set_position(LogicalPosition::new(x, y)).map_err(|e| e.to_string())?;
        wv.set_size(LogicalSize::new(width, height)).map_err(|e| e.to_string())?;
        wv.show().map_err(|e| e.to_string())
    } else {
        wv.hide().map_err(|e| e.to_string())?;
        // It had the keyboard: VEXT's own page gets it back (Space plays and
        // pauses there), else keys would go on reaching the hidden page.
        if browser.focused.swap(false, Ordering::SeqCst) {
            if let Some(main) = app.get_webview("main") {
                let _ = main.set_focus();
            }
        }
        Ok(())
    }
}

#[tauri::command]
pub async fn browser_go(app: AppHandle, action: String, url: Option<String>) -> Result<(), String> {
    let wv = app.get_webview(LABEL).ok_or("the browser isn't open")?;
    let native = |f: fn(&ICoreWebView2)| {
        wv.with_webview(move |platform| unsafe {
            if let Ok(core) = platform.controller().CoreWebView2() {
                f(&core);
            }
        })
        .map_err(|e| e.to_string())
    };
    match action.as_str() {
        "back" => native(|core| unsafe {
            let _ = core.GoBack();
        }),
        "forward" => native(|core| unsafe {
            let _ = core.GoForward();
        }),
        "stop" => native(|core| unsafe {
            let _ = core.Stop();
        }),
        "reload" => wv.reload().map_err(|e| e.to_string()),
        "home" => wv.navigate(HOME.parse().unwrap()).map_err(|e| e.to_string()),
        "go" => wv.navigate(address(url.as_deref().unwrap_or(""))?).map_err(|e| e.to_string()),
        _ => Err(format!("unknown browser action: {action}")),
    }
}

#[tauri::command]
pub async fn browser_state(app: AppHandle) -> BrowserState {
    app.state::<Browser>().state.lock().unwrap().clone()
}

#[cfg(test)]
mod tests {
    use super::address;

    #[test]
    fn addresses() {
        assert_eq!(address("https://example.com/a").unwrap().as_str(), "https://example.com/a");
        assert_eq!(address("youtube.com/watch?v=1").unwrap().as_str(), "https://youtube.com/watch?v=1");
        assert_eq!(address("localhost:3000").unwrap().as_str(), "http://localhost:3000/");
        assert!(address("lo-fi beats").unwrap().as_str().starts_with("https://duckduckgo.com/?q=lo-fi+beats"));
        assert!(address("C++").unwrap().as_str().starts_with("https://duckduckgo.com/?q="));
        assert!(address("file:///C:/x").is_err());
        assert!(address("http://tauri.localhost/").is_err());
        assert!(address("").is_err());
    }
}
