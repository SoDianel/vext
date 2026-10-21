// The plugin sheet's image browser ("Search images…", and "No AI: search images
// in VEXT"): a third WebView2 inside the main window, showing Google Images.
// Same technique as the Playground's browser (browser.rs): the main page draws
// the modal and its chrome (search box, Back / Forward / Reload), reserves a
// rectangle and calls imgsearch_place with it; this file owns the webview.
// Unlike the Playground's browser it lives only while the modal is open
// (imgsearch_close destroys it).
//
// Picking: imgsearch_pick.js (injected into the top frame) puts a "Use this
// image" button over the image under the mouse, resolves Google's thumbnails to
// the original image when the preview panel shows it, and calls imgsearch_pick.
// That is the only command this webview may call (capabilities/imgsearch.json,
// webview "imgsearch", remote URLs): it shows remote pages, so it never gets the
// VEXT API token or any other command. imgsearch_pick only hands the addresses
// to the main page (event imgsearch://pick); the main page downloads it through
// the backend (same safety rules as a pasted link) and the owner still has to
// press Apply in the picker.
//
// Commands that create or move the child webview MUST be async (see browser.rs).
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Url, WebviewBuilder, WebviewUrl};
use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2;
use webview2_com::{
    take_pwstr, HistoryChangedEventHandler, NavigationCompletedEventHandler, NavigationStartingEventHandler,
    SourceChangedEventHandler,
};
use windows::core::{BOOL, PWSTR};

pub const LABEL: &str = "imgsearch";
const SEARCH: &str = "https://www.google.com/search";
const PICK_JS: &str = include_str!("imgsearch_pick.js");
const STATE_EVENT: &str = "imgsearch://state";
const PICK_EVENT: &str = "imgsearch://pick";
// A data: thumbnail (Google inlines the first rows' thumbnails) is a few KB;
// anything near this is not a thumbnail.
const MAX_DATA_URL: usize = 1_500_000;
const MAX_URL: usize = 8192;

#[derive(Clone, Default, PartialEq, Serialize)]
pub struct State {
    url: String,
    can_go_back: bool,
    can_go_forward: bool,
    loading: bool,
}

#[derive(Clone, Serialize)]
struct Pick {
    image_url: String,
    page_url: Option<String>,
    thumbnail: bool,
}

#[derive(Default)]
pub struct ImgSearch {
    state: Mutex<State>,
    last_pick: Mutex<Option<Instant>>,
    // Serializes imgsearch_place (creation must not race; later calls apply in order).
    place: tauri::async_runtime::Mutex<()>,
}

// Web pages only: not VEXT's own pages (*.localhost, Tauri's IPC treats them as
// local), not file: or other schemes that hand off to another app.
fn allowed_navigation(url: &Url) -> bool {
    match url.scheme() {
        "http" | "https" => !url.host_str().is_some_and(|h| h.to_ascii_lowercase().ends_with(".localhost")),
        "about" => true,
        _ => false,
    }
}

// Google Images for the text, or the address itself when the owner typed a
// web address. (tbm=isch now redirects to udm=2, Google's Images tab.)
fn search_url(text: &str) -> Result<Url, String> {
    let text = text.trim();
    if text.is_empty() {
        return Err("type what to search for".into());
    }
    if !text.contains(char::is_whitespace) && (text.starts_with("https://") || text.starts_with("http://")) {
        if let Ok(url) = Url::parse(text) {
            if allowed_navigation(&url) {
                return Ok(url);
            }
        }
    }
    Url::parse_with_params(SEARCH, &[("q", text), ("udm", "2")]).map_err(|e| e.to_string())
}

fn update(app: &AppHandle, core: Option<ICoreWebView2>, loading: Option<bool>) {
    let snapshot = {
        let me = app.state::<ImgSearch>();
        let mut s = me.state.lock().unwrap();
        let before = s.clone();
        if let Some(core) = &core {
            unsafe {
                let mut text = PWSTR::null();
                if core.Source(&mut text).is_ok() {
                    s.url = take_pwstr(text);
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

// Native events: they also cover same-document navigations (Google's preview
// panel changes the address with pushState).
fn watch(app: AppHandle, webview: &tauri::Webview) -> tauri::Result<()> {
    webview.with_webview(move |platform| unsafe {
        let Ok(core) = platform.controller().CoreWebView2() else { return };
        let mut token = 0i64;
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
    })
}

fn create(app: &AppHandle, start: Url, x: f64, y: f64, width: f64, height: f64) -> Result<(), String> {
    let window = app.get_window("main").ok_or("main window not found")?;
    *app.state::<ImgSearch>().state.lock().unwrap() = State::default();
    let nav = app.clone();
    let builder = WebviewBuilder::new(LABEL, WebviewUrl::External(start))
        // Top frame only: the picks come from the page the owner looks at.
        .initialization_script(PICK_JS)
        // Identical to every other webview's (see crate::WEBVIEW_ARGS): one WebView2 environment.
        .additional_browser_args(crate::WEBVIEW_ARGS)
        .on_navigation(allowed_navigation)
        // "Open in a new tab" links open here: one view, with Back to return.
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
        // No downloads: pictures go through VEXT's own download (safety rules).
        .on_download(|_, event| !matches!(event, DownloadEvent::Requested { .. }));
    // Profile: the default WebView2 user data folder of the main window (as the
    // Playground's browser), which persists: Google's consent choice is kept.
    let webview = window
        .add_child(builder, LogicalPosition::new(x, y), LogicalSize::new(width, height))
        .map_err(|e| e.to_string())?;
    watch(app.clone(), &webview).map_err(|e| e.to_string())
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
pub async fn imgsearch_place(
    app: AppHandle,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    visible: bool,
    query: Option<String>,
) -> Result<(), String> {
    let me = app.state::<ImgSearch>();
    let _serial = me.place.lock().await;
    let (width, height) = (width.max(1.0), height.max(1.0));
    let Some(wv) = app.get_webview(LABEL) else {
        if !visible {
            return Ok(());
        }
        let start = search_url(query.as_deref().unwrap_or(""))?;
        return create(&app, start, x, y, width, height);
    };
    if visible {
        wv.set_position(LogicalPosition::new(x, y)).map_err(|e| e.to_string())?;
        wv.set_size(LogicalSize::new(width, height)).map_err(|e| e.to_string())?;
        wv.show().map_err(|e| e.to_string())
    } else {
        wv.hide().map_err(|e| e.to_string())
    }
}

#[tauri::command]
pub async fn imgsearch_go(app: AppHandle, action: String, query: Option<String>) -> Result<(), String> {
    let wv = app.get_webview(LABEL).ok_or("the image browser isn't open")?;
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
        "reload" => wv.reload().map_err(|e| e.to_string()),
        "search" => wv.navigate(search_url(query.as_deref().unwrap_or(""))?).map_err(|e| e.to_string()),
        _ => Err(format!("unknown action: {action}")),
    }
}

#[tauri::command]
pub async fn imgsearch_close(app: AppHandle) -> Result<(), String> {
    let me = app.state::<ImgSearch>();
    let _serial = me.place.lock().await;
    if let Some(wv) = app.get_webview(LABEL) {
        wv.close().map_err(|e| e.to_string())?;
        // The close is queued on the UI thread: wait for it, so an image browser
        // opened again right away creates a new webview instead of finding this one.
        for _ in 0..50 {
            if app.get_webview(LABEL).is_none() {
                break;
            }
            let _ = tauri::async_runtime::spawn_blocking(|| std::thread::sleep(Duration::from_millis(20))).await;
        }
    }
    *me.state.lock().unwrap() = State::default();
    Ok(())
}

fn web_url(text: &str) -> Option<String> {
    let url = Url::parse(text).ok()?;
    (text.len() <= MAX_URL && matches!(url.scheme(), "http" | "https") && allowed_navigation(&url))
        .then(|| url.to_string())
}

// The only command the image browser's pages can call. Whatever page asks, the
// result is the same: the address goes to the main page, which downloads it
// with the backend's safety rules and shows it in the picker, where nothing is
// applied until the owner presses Apply.
#[tauri::command]
pub async fn imgsearch_pick(
    app: AppHandle,
    webview: tauri::Webview,
    image_url: String,
    page_url: Option<String>,
    thumbnail: Option<bool>,
) -> Result<(), String> {
    if webview.label() != LABEL {
        return Err("not allowed".into());
    }
    {
        let me = app.state::<ImgSearch>();
        let mut last = me.last_pick.lock().unwrap();
        if last.is_some_and(|t| t.elapsed() < Duration::from_millis(700)) {
            return Err("one at a time".into());
        }
        *last = Some(Instant::now());
    }
    let lower = image_url.get(..16).unwrap_or(&image_url).to_ascii_lowercase();
    let image_url = if lower.starts_with("data:image/") {
        if image_url.len() > MAX_DATA_URL {
            return Err("that image is too large".into());
        }
        image_url
    } else {
        web_url(&image_url).ok_or("not a web image address")?
    };
    let page_url = page_url.as_deref().and_then(web_url);
    app.emit_to("main", PICK_EVENT, Pick { image_url, page_url, thumbnail: thumbnail.unwrap_or(false) })
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::search_url;

    #[test]
    fn searches() {
        let u = search_url(" FabFilter Pro-Q 3 plugin ").unwrap();
        assert_eq!(u.host_str(), Some("www.google.com"));
        assert!(u.as_str().contains("udm=2"));
        assert!(u.as_str().contains("q=FabFilter+Pro-Q+3+plugin"));
        assert_eq!(search_url("https://example.com/x.png").unwrap().as_str(), "https://example.com/x.png");
        assert!(search_url("http://tauri.localhost/").unwrap().host_str() == Some("www.google.com"));
        assert!(search_url("   ").is_err());
    }
}
