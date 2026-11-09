// VEXT's bubble mode ("bubble"): one always-on-top window with no owner, replacing
// the hidden main window. Its fixed canvas holds the circle and, on hover, the
// FL preset index panel beside it. A native region clips the canvas to the circle, or to the
// circle plus the panel, so the rest of the canvas does not intercept the desktop. The page
// is static/bubble.html (copy-frontend.js mirrors app/static/ under dist-mirror/static/).
//
// The bubble's centre is the anchor. Its window is pulled inside the work area of the
// monitor under that centre, so near an edge the centre moves within the window instead
// of the window running off screen. Bubble geometry changes run on the UI thread.
// No state lock is held across a window call: window calls can dispatch events that read it.
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::webview::NewWindowResponse;
use tauri::{
    AppHandle, Emitter, Manager, Monitor, WebviewUrl, WebviewWindow, WebviewWindowBuilder, Window,
    WindowEvent, Wry,
};

const MAIN: &str = "main";
const BUBBLE: &str = "bubble";
const BUBBLE_PAGE: &str = "static/bubble.html";
// Logical px. The circle's radius including its ring, the gap between it and the panel, the
// margin kept around both, and the panel's largest and smallest size.
const R: f64 = 36.0;
const GAP: f64 = 10.0;
const MARGIN: f64 = 8.0;
const PANEL_MAX: (f64, f64) = (880.0, 600.0);
const PANEL_MIN: (f64, f64) = (520.0, 360.0);
// The default centre: this far in from the primary work area's right edge, at mid-height.
const DEFAULT_INSET: f64 = 100.0;
// Moves this soon after the shell's own resize or move are not the user's.
const SHELL_MOVE: Duration = Duration::from_millis(300);
const NO_MONITOR: &str = "No monitor is available";

// Serialises creating and opening, so two quick clicks cannot build a window twice.
#[derive(Default)]
pub struct Bubble {
    serial: tauri::async_runtime::Mutex<()>,
    state: Mutex<State>,
}

struct State {
    active: bool,
    // Logical px from the bubble window's top-left to its centre.
    offset: (f64, f64),
    // Logical px. The window's size, fixed until the next reflow.
    canvas: (f64, f64),
    // The current layout; the center can be dragged in either layout.
    expanded: bool,
    shell_move_until: Option<Instant>,
}

impl Default for State {
    fn default() -> Self {
        Self {
            active: false,
            offset: (0.0, 0.0),
            canvas: desired_canvas(),
            expanded: false,
            shell_move_until: None,
        }
    }
}

#[derive(Serialize)]
pub struct Centre {
    cx: f64,
    cy: f64,
}

// What bubble_expand returns: the centre and the panel, logical px from the canvas's top-left.
#[derive(Serialize)]
pub struct Geometry {
    cx: f64,
    cy: f64,
    panel: Rect,
    side: &'static str,
}

// The index panel inside the canvas, logical px.
#[derive(Serialize, Clone, Copy, Debug, PartialEq)]
pub struct Rect {
    x: f64,
    y: f64,
    w: f64,
    h: f64,
}

// Physical px, as integers. `Area` is a monitor's work area.
#[derive(Clone, Copy)]
struct Area {
    x: i32,
    y: i32,
    w: i32,
    h: i32,
}

fn area_of(monitor: &Monitor) -> Area {
    let area = monitor.work_area();
    Area {
        x: area.position.x,
        y: area.position.y,
        w: area.size.width as i32,
        h: area.size.height as i32,
    }
}

fn contains(area: Area, (x, y): (i32, i32)) -> bool {
    x >= area.x && x < area.x + area.w && y >= area.y && y < area.y + area.h
}

// `value` moved so that [value, value + size) lies in [start, start + span), left or top edge first.
fn fit(value: i32, start: i32, span: i32, size: i32) -> i32 {
    value.min(start + span - size).max(start)
}

// Position of a window of `size` whose `offset` point should sit on `centre`, pulled
// inside `area`. Returns that position and the offset it leaves.
fn place(
    area: Area,
    centre: (i32, i32),
    size: (i32, i32),
    offset: (i32, i32),
) -> ((i32, i32), (i32, i32)) {
    let x = fit(centre.0 - offset.0, area.x, area.w, size.0);
    let y = fit(centre.1 - offset.1, area.y, area.h, size.1);
    ((x, y), (centre.0 - x, centre.1 - y))
}

fn physical(logical: (f64, f64), scale: f64) -> (i32, i32) {
    (
        (logical.0 * scale).round() as i32,
        (logical.1 * scale).round() as i32,
    )
}

// The canvas at its largest: the panel beside the circle, with the margins around both.
fn desired_canvas() -> (f64, f64) {
    (PANEL_MAX.0 + GAP + 2.0 * R + 2.0 * MARGIN, PANEL_MAX.1 + 2.0 * MARGIN)
}

// The desired canvas, capped to the work area in logical px.
fn canvas_for(area: Area, scale: f64) -> (f64, f64) {
    let (w, h) = desired_canvas();
    (w.min(area.w as f64 / scale), h.min(area.h as f64 / scale))
}

// Where the circle's centre sits in the canvas. The right column (the panel opens left) is
// used when the canvas fits left of the centre inside the work area, else the left column.
fn anchor_offset(area: Area, centre: (i32, i32), canvas: (f64, f64), scale: f64) -> (f64, f64) {
    let right = (canvas.0 - MARGIN - R, canvas.1 / 2.0);
    if centre.0 - physical(right, scale).0 >= area.x {
        right
    } else {
        (MARGIN + R, canvas.1 / 2.0)
    }
}

// The panel beside the circle, on the side with more room, and that side ("left" or "right").
// The bounds are applied with max then min, so a canvas smaller than the margins cannot panic.
fn panel_layout(canvas: (f64, f64), offset: (f64, f64)) -> (Rect, &'static str) {
    let (width, height) = canvas;
    let (cx, cy) = offset;
    let left_room = cx - R - GAP - MARGIN;
    let right_room = width - MARGIN - (cx + R + GAP);
    let left = left_room >= right_room;
    let room = if left { left_room } else { right_room };
    let w = room
        .clamp(PANEL_MIN.0, PANEL_MAX.0)
        .min(width - 2.0 * MARGIN)
        .max(0.0);
    let x = if left { cx - R - GAP - w } else { cx + R + GAP };
    let x = x.max(MARGIN).min(width - MARGIN - w);
    let h = PANEL_MAX.1.min(height - 2.0 * MARGIN).max(0.0);
    let y = (cy - h / 2.0).max(MARGIN).min(height - MARGIN - h);
    (Rect { x, y, w, h }, if left { "left" } else { "right" })
}

// The monitor under a physical point, else the primary one.
fn monitor_at(app: &AppHandle, point: (i32, i32)) -> Option<Monitor> {
    app.monitor_from_point(point.0 as f64, point.1 as f64)
        .ok()
        .flatten()
        .or_else(|| app.primary_monitor().ok().flatten())
}

fn default_centre(app: &AppHandle) -> Option<(i32, i32)> {
    let monitor = app.primary_monitor().ok().flatten()?;
    let area = area_of(&monitor);
    let inset = (DEFAULT_INSET * monitor.scale_factor()).round() as i32;
    Some((area.x + area.w - inset, area.y + area.h / 2))
}

#[derive(Serialize, Deserialize)]
struct Saved {
    x: i32,
    y: i32,
}

fn saved_file(app: &AppHandle) -> Option<PathBuf> {
    Some(app.path().app_config_dir().ok()?.join("bubble.json"))
}

// The saved centre, if it is still on a monitor (a disconnected one falls back to the default).
fn load_centre(app: &AppHandle) -> Option<(i32, i32)> {
    let file = saved_file(app)?;
    // Retain the anchor saved before bubble mode became an app-wide feature.
    let text = std::fs::read_to_string(&file)
        .or_else(|_| std::fs::read_to_string(file.with_file_name("fl-bubble.json")))
        .ok()?;
    let saved: Saved = serde_json::from_str(&text).ok()?;
    let point = (saved.x, saved.y);
    let on_screen = app
        .available_monitors()
        .ok()?
        .iter()
        .any(|m| contains(area_of(m), point));
    on_screen.then_some(point)
}

fn save_centre(app: &AppHandle, centre: (i32, i32)) {
    let Some(file) = saved_file(app) else {
        return;
    };
    if let Some(dir) = file.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::write(
        file,
        serde_json::json!({"x": centre.0, "y": centre.1}).to_string(),
    );
}

// Marks the moves that follow as the shell's own, not the user's.
fn shell_moving(app: &AppHandle) {
    app.state::<Bubble>()
        .state
        .lock()
        .unwrap()
        .shell_move_until = Some(Instant::now() + SHELL_MOVE);
}

// Size and position in one SetWindowPos. Two calls (size, then move) showed the window grown
// from its old corner for a moment, and the page saw the pointer leave and collapsed the menu.
fn place_bubble(
    app: &AppHandle,
    window: &WebviewWindow,
    size: (i32, i32),
    position: (i32, i32),
) -> Result<(), String> {
    use windows::Win32::UI::WindowsAndMessaging::{SetWindowPos, SWP_NOACTIVATE, SWP_NOZORDER};
    shell_moving(app);
    let hwnd = window.hwnd().map_err(|e| e.to_string())?;
    unsafe {
        SetWindowPos(
            hwnd,
            None,
            position.0,
            position.1,
            size.0,
            size.1,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
    }
    .map_err(|e| e.to_string())
}

// The canvas never resizes on hover. The native region keeps the bubble from intercepting
// the desktop around it: the circle when closed, the circle and the panel when open. Windows
// owns the region after a successful SetWindowRgn; the parts are deleted here when it does not.
fn clip_bubble(
    window: &WebviewWindow,
    offset: (f64, f64),
    canvas: (f64, f64),
    expanded: bool,
) -> Result<(), String> {
    use windows::Win32::Graphics::Gdi::{
        CombineRgn, CreateEllipticRgn, CreateRoundRectRgn, DeleteObject, SetWindowRgn, RGN_ERROR,
        RGN_OR,
    };
    let hwnd = window.hwnd().map_err(|e| e.to_string())?;
    let scale = window.scale_factor().map_err(|e| e.to_string())?;
    let at = physical(offset, scale);
    let radius = (R * scale).round() as i32;
    let region = unsafe { CreateEllipticRgn(at.0 - radius, at.1 - radius, at.0 + radius, at.1 + radius) };
    if region.0.is_null() {
        return Err("Could not create the bubble surface".into());
    }
    if expanded {
        let (panel, _) = panel_layout(canvas, offset);
        let top_left = physical((panel.x, panel.y), scale);
        let bottom_right = physical((panel.x + panel.w, panel.y + panel.h), scale);
        // The rounding arguments are ellipse sizes, so 24 px gives a 12 px corner.
        let rounding = (24.0 * scale).round() as i32;
        let panel_region = unsafe {
            CreateRoundRectRgn(top_left.0, top_left.1, bottom_right.0, bottom_right.1, rounding, rounding)
        };
        let combined = if panel_region.0.is_null() {
            RGN_ERROR
        } else {
            unsafe { CombineRgn(Some(region), Some(region), Some(panel_region), RGN_OR) }
        };
        if !panel_region.0.is_null() {
            let _ = unsafe { DeleteObject(panel_region.into()) };
        }
        if combined == RGN_ERROR {
            let _ = unsafe { DeleteObject(region.into()) };
            return Err("Could not combine the bubble surface".into());
        }
    }
    if unsafe { SetWindowRgn(hwnd, Some(region), true) } == 0 {
        let _ = unsafe { DeleteObject(region.into()) };
        return Err("Could not close the bubble surface".into());
    }
    Ok(())
}

// The hardening of plugin_frame::create_window: VEXT's own page only, no new windows,
// the same WebView2 arguments, and window.__VEXT__.
fn harden(
    builder: WebviewWindowBuilder<'_, Wry, AppHandle>,
) -> WebviewWindowBuilder<'_, Wry, AppHandle> {
    builder
        .initialization_script(crate::config().init_script())
        .additional_browser_args(crate::WEBVIEW_ARGS)
        .on_navigation(|url| url.host_str() == Some("tauri.localhost"))
        .on_new_window(|_, _| NewWindowResponse::Deny)
}

fn create_bubble(app: &AppHandle) -> Result<(), String> {
    let centre = load_centre(app)
        .or_else(|| default_centre(app))
        .ok_or(NO_MONITOR)?;
    let monitor = monitor_at(app, centre).ok_or(NO_MONITOR)?;
    let scale = monitor.scale_factor();
    let area = area_of(&monitor);
    let canvas = canvas_for(area, scale);
    let (position, anchor) = place(
        area,
        centre,
        physical(canvas, scale),
        physical(anchor_offset(area, centre, canvas, scale), scale),
    );
    let offset = (anchor.0 as f64 / scale, anchor.1 as f64 / scale);
    *app.state::<Bubble>().state.lock().unwrap() = State {
        offset,
        canvas,
        ..State::default()
    };
    // Built hidden: the window is placed before it shows, and the moves of building it are not taken as the user's.
    let window = harden(WebviewWindowBuilder::new(
        app,
        BUBBLE,
        WebviewUrl::App(BUBBLE_PAGE.into()),
    ))
    .title("VEXT bubble")
    .transparent(true)
    .decorations(false)
    .shadow(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .resizable(false)
    .focused(false)
    .visible(false)
    .inner_size(canvas.0, canvas.1)
    .build()
    .map_err(|e| e.to_string())?;
    if let Err(error) = place_bubble(app, &window, physical(canvas, scale), position)
        .and_then(|_| clip_bubble(&window, offset, canvas, false))
    {
        let _ = window.destroy();
        return Err(error);
    }
    Ok(())
}

fn require(window: &WebviewWindow, label: &str, message: &str) -> Result<(), String> {
    if window.label() == label {
        Ok(())
    } else {
        Err(message.into())
    }
}

#[tauri::command]
pub async fn bubble_enter(webview: tauri::Webview) -> Result<(), String> {
    // Adding the Playground browser makes main a multi-webview window. Tauri
    // then rejects WebviewWindow command arguments, even from the main page.
    let window = webview.window();
    if webview.label() != MAIN || window.label() != MAIN {
        return Err("Only the main VEXT page may show the VEXT bubble".into());
    }
    let app = webview.app_handle().clone();
    let shared = app.state::<Bubble>();
    let _serial = shared.serial.lock().await;
    if app.get_webview_window(BUBBLE).is_none() {
        create_bubble(&app)?;
    }
    let bubble = app.get_webview_window(BUBBLE).ok_or("Bubble window unavailable")?;
    bubble.show().map_err(|e| e.to_string())?;
    shared.state.lock().unwrap().active = true;
    if let Err(error) = window.hide() {
        shared.state.lock().unwrap().active = false;
        let _ = bubble.hide();
        return Err(error.to_string());
    }
    Ok(())
}

// Showing the existing main webview preserves its page, dialogs and Playground session.
// This also handles a second launch while VEXT is in bubble mode.
pub fn restore_main(app: &AppHandle) -> Result<(), String> {
    let main = app.get_window(MAIN).ok_or("VEXT window unavailable")?;
    main.unminimize().map_err(|e| e.to_string())?;
    main.show().map_err(|e| e.to_string())?;
    main.set_focus().map_err(|e| e.to_string())?;
    app.state::<Bubble>().state.lock().unwrap().active = false;
    if let Some(bubble) = app.get_webview_window(BUBBLE) {
        bubble.destroy().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub async fn bubble_restore(window: WebviewWindow) -> Result<(), String> {
    require(&window, BUBBLE, "Only the VEXT bubble may restore the app")?;
    let app = window.app_handle().clone();
    let shared = app.state::<Bubble>();
    let _serial = shared.serial.lock().await;
    restore_main(&app)
}

#[tauri::command]
pub async fn bubble_expand(window: WebviewWindow, expanded: bool, reflow: Option<bool>) -> Result<Geometry, String> {
    require(&window, BUBBLE, "Only the VEXT bubble may resize itself")?;
    let app = window.app_handle().clone();
    let shared = app.state::<Bubble>();
    let _serial = shared.serial.lock().await;
    if reflow.unwrap_or(false) {
        // Tauri posts the OS drag message and returns before button release.
        // Waiting off the UI thread leaves the native drag loop free to finish.
        tauri::async_runtime::spawn_blocking(|| {
            use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON};
            while unsafe { GetAsyncKeyState(VK_LBUTTON.0 as i32) } < 0 {
                std::thread::sleep(Duration::from_millis(16));
            }
        })
        .await
        .map_err(|e| e.to_string())?;
    }
    let (sender, receiver) = std::sync::mpsc::channel();
    let bubble = window.clone();
    // SetWindowPos dispatches window messages synchronously; keep the whole transaction
    // on the window thread and acknowledge only after the native operation succeeds.
    window
        .run_on_main_thread(move || {
            let _ = sender.send(resize_bubble(&bubble, expanded, reflow.unwrap_or(false)));
        })
        .map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || receiver.recv().map_err(|e| e.to_string())?)
        .await
        .map_err(|e| e.to_string())?
}

fn resize_bubble(window: &WebviewWindow, expanded: bool, reflow: bool) -> Result<Geometry, String> {
    let app = window.app_handle().clone();
    let shared = app.state::<Bubble>();
    let (mut offset, mut canvas) = {
        let state = shared.state.lock().unwrap();
        (state.offset, state.canvas)
    };
    if reflow {
        let scale = window.scale_factor().map_err(|e| e.to_string())?;
        let origin = window.outer_position().map_err(|e| e.to_string())?;
        let anchor = physical(offset, scale);
        let centre = (origin.x + anchor.0, origin.y + anchor.1);
        let monitor = monitor_at(&app, centre).ok_or(NO_MONITOR)?;
        let scale = monitor.scale_factor();
        let area = area_of(&monitor);
        canvas = canvas_for(area, scale);
        let (position, placed) = place(
            area,
            centre,
            physical(canvas, scale),
            physical(anchor_offset(area, centre, canvas, scale), scale),
        );
        place_bubble(&app, window, physical(canvas, scale), position)?;
        offset = (placed.0 as f64 / scale, placed.1 as f64 / scale);
    }
    clip_bubble(window, offset, canvas, expanded)?;
    {
        let mut state = shared.state.lock().unwrap();
        state.expanded = expanded;
        state.offset = offset;
        state.canvas = canvas;
    }
    let (panel, side) = panel_layout(canvas, offset);
    Ok(Geometry {
        cx: offset.0,
        cy: offset.1,
        panel,
        side,
    })
}

// The cursor relative to the bubble window, logical px. The expanded menu closes from this,
// not from DOM mouseleave: a transparent window that resizes under the pointer reports
// spurious leaves, and its transparent gaps may not be hit-tested at all.
#[tauri::command]
pub async fn bubble_cursor(window: WebviewWindow) -> Result<Centre, String> {
    require(&window, BUBBLE, "Only the VEXT bubble may read the cursor")?;
    let scale = window.scale_factor().map_err(|e| e.to_string())?;
    let origin = window.outer_position().map_err(|e| e.to_string())?;
    let cursor = window.cursor_position().map_err(|e| e.to_string())?;
    Ok(Centre {
        cx: (cursor.x - origin.x as f64) / scale,
        cy: (cursor.y - origin.y as f64) / scale,
    })
}

// Window events (main thread). lib.rs calls this before it stops the backend: the
// bubble has no owner, and VEXT cannot exit while its windows exist.
pub fn on_window_event(window: &Window, event: &WindowEvent) {
    let app = window.app_handle();
    match (window.label(), event) {
        (BUBBLE, WindowEvent::Moved(at)) => {
            let shared = app.state::<Bubble>();
            let (offset, shell) = {
                let state = shared.state.lock().unwrap();
                (
                    state.offset,
                    state
                        .shell_move_until
                        .is_some_and(|until| Instant::now() < until),
                )
            };
            // Hidden while it is built; minimized, it is parked off screen by Windows.
            let user_move = !shell
                && window.is_visible().unwrap_or(false)
                && !window.is_minimized().unwrap_or(true);
            if !user_move {
                return;
            }
            let scale = window.scale_factor().unwrap_or(1.0);
            let offset = physical(offset, scale);
            save_centre(app, (at.x + offset.0, at.y + offset.1));
            let _ = app.emit_to(BUBBLE, "bubble:moved", ());
        }
        (MAIN, WindowEvent::Destroyed) => {
            app.state::<Bubble>().state.lock().unwrap().active = false;
            if let Some(bubble) = app.get_webview_window(BUBBLE) {
                let _ = bubble.destroy();
            }
        }
        (BUBBLE, WindowEvent::CloseRequested { api, .. }) => {
            api.prevent_close();
            let _ = restore_main(app);
        }
        (BUBBLE, WindowEvent::Destroyed) => {
            let active = app.state::<Bubble>().state.lock().unwrap().active;
            if active {
                let _ = restore_main(app);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reflow_preserves_the_anchor_and_fits_the_canvas_at_both_dpi_scales() {
        for scale in [1.0, 1.5] {
            let area = Area { x: -1920, y: 0, w: 1920, h: 1080 };
            for centre in [(-1900, 40), (-30, 1000), (-960, 500)] {
                let canvas = canvas_for(area, scale);
                let size = physical(canvas, scale);
                let anchor = physical(anchor_offset(area, centre, canvas, scale), scale);
                let (position, offset) = place(area, centre, size, anchor);
                assert_eq!((position.0 + offset.0, position.1 + offset.1), centre);
                assert!(position.0 >= area.x && position.0 + size.0 <= area.x + area.w);
                assert!(position.1 >= area.y && position.1 + size.1 <= area.y + area.h);
            }
        }
    }

    #[test]
    fn open_panel_stays_inside_the_canvas_and_clears_the_circle() {
        let areas = [
            Area { x: -1920, y: 0, w: 1920, h: 1080 },
            Area { x: 0, y: 0, w: 1280, h: 720 },
        ];
        for area in areas {
            for scale in [1.0, 1.5] {
                let canvas = canvas_for(area, scale);
                let (left, right) = (area.x + 10, area.x + area.w - 10);
                let (top, bottom) = (area.y + 5, area.y + area.h - 5);
                let (mid_x, mid_y) = (area.x + area.w / 2, area.y + area.h / 2);
                for centre in [(left, mid_y), (right, mid_y), (mid_x, mid_y), (mid_x, top), (mid_x, bottom)] {
                    let anchor = physical(anchor_offset(area, centre, canvas, scale), scale);
                    let (_, placed) = place(area, centre, physical(canvas, scale), anchor);
                    let offset = (placed.0 as f64 / scale, placed.1 as f64 / scale);
                    let (panel, _) = panel_layout(canvas, offset);
                    assert!(
                        panel.x >= 0.0 && panel.y >= 0.0,
                        "panel leaves the canvas at {centre:?}, scale {scale}"
                    );
                    assert!(
                        panel.x + panel.w <= canvas.0 && panel.y + panel.h <= canvas.1,
                        "panel leaves the canvas at {centre:?}, scale {scale}"
                    );
                    if canvas == desired_canvas() {
                        assert!(
                            panel.x + panel.w <= offset.0 - R || panel.x >= offset.0 + R,
                            "panel overlaps the circle at {centre:?}, scale {scale}"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn panel_layout_never_panics_on_a_tiny_canvas() {
        for canvas in [(300.0, 200.0), (10.0, 10.0)] {
            for offset in [(0.0, 0.0), (150.0, 100.0), (300.0, 200.0), (-50.0, 500.0)] {
                let (panel, side) = panel_layout(canvas, offset);
                assert!(side == "left" || side == "right");
                assert!(panel.w >= 0.0 && panel.h >= 0.0);
                assert!(panel.x + panel.w <= canvas.0 && panel.y + panel.h <= canvas.1);
            }
        }
    }
}
