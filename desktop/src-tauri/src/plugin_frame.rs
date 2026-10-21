// The plugin frame: VEXT's container for an open plugin's editor.
//
// The editor is a real window of the plugin's worker process (pedalboard /
// JUCE, see app/host_worker.py). It can't become a child of a VEXT window:
// reparenting it (SetParent, WS_CHILD) makes pedalboard close it. So the frame
// is a small top-level window of its own, "plugin-frame", owned by the main
// window, and the editor becomes an owned window of the frame
// (GWLP_HWNDPARENT). An owned window always stays above its owner and hides
// with it, so the z-order is main < frame < editor and the whole chain
// follows the main window. The editor keeps its own caption and borders; a
// window region shows only its client area, which is what gets placed on the
// frame (see Editor::adopt for why the caption isn't stripped). The frame's page
// (app/static/plugin-frame.html) draws the title bar (name, Capture menu,
// minimize, full screen, close) at the top, the dark body the editor sits on,
// and the control bar at the bottom (IN / OUT knobs, Auto-Balance, Bypass);
// this file keeps the editor on that body, between the two bars, and the frame
// inside the main window.
//
// Geometry is in physical pixels throughout (VEXT and the worker are both
// per-monitor DPI aware). Only the two bars' heights (and the narrowest width)
// are logical: the page's CSS sizes times the frame's DPI scale.
//
// All window work runs on the main (UI) thread: the commands hop over with
// on_main, window events already run there. A worker thread moving the frame
// would send its messages to the UI thread and wait, and the UI thread could
// be waiting for the state lock: a deadlock. The state lock is never held
// while windows are moved (moving them can dispatch window events).
//
// Commands that create or move windows MUST be async: a sync command runs on
// the UI thread, and building a window waits for that same thread.
//
// No flash (2026-10-15): the Playground opens plugins with framed=true, so the
// worker keeps the editor DWM-cloaked (on no screen) until this file has put it
// in the frame and shown the frame; relayout then posts REVEAL_MESSAGE to the
// editor and the worker uncloaks it (host_worker._Hold; a window can only be
// cloaked by its own process). Before, the editor showed at the screen's
// top-left for ~0.2 s, then jumped into the frame.
//
// Resizing (2026-10-15): a resizable editor (sizing border) makes the frame
// resizable: dragging the frame's edges resizes the editor, and WM_SIZING
// (frame_subclass) snaps the drag to the size the plugin accepted (JUCE
// constrains in WM_WINDOWPOSCHANGING, VEXT's own editor window in WM_SIZE), so
// the frame never shows a gap. A plugin that resizes its own window (an
// in-editor resize corner) is followed within one POLL. Both stay inside the
// main window's client area.
//
// Scrolling (2026-10-15): an editor bigger than the room the main window
// leaves (a fixed-size editor taller or wider than its client area minus the
// bars) keeps its size; the frame stops at the main window's client area, both
// bars stay visible, and the body becomes a viewport with scroll bars that the
// page draws in gutters beside it (plugin-frame.css, SCROLLBAR wide). Scrolling
// moves the editor window behind the viewport and its window region shows only
// the part inside it (clip); the bypass overlay covers that visible part.
// PrintWindow still renders the whole editor (captures are the full editor).
use std::sync::atomic::{AtomicIsize, AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use serde::Serialize;
use tauri::webview::{NewWindowResponse, PageLoadEvent};
use tauri::{AppHandle, Emitter, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, Window, WindowEvent};
use windows::core::w;
use windows::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
use windows::Win32::Graphics::Dwm::{
    DwmSetWindowAttribute, DWMNCRENDERINGPOLICY, DWMNCRP_DISABLED, DWMNCRP_USEWINDOWSTYLE, DWMWA_NCRENDERING_POLICY,
};
use windows::Win32::Graphics::Gdi::{
    BeginPaint, ClientToScreen, CreateFontW, CreateRectRgn, CreateSolidBrush, DeleteObject, DrawTextW, EndPaint,
    FillRect, GetWindowRgnBox, InvalidateRect, SelectObject, SetBkMode, SetTextColor, SetWindowRgn, CLEARTYPE_QUALITY,
    CLIP_DEFAULT_PRECIS, DEFAULT_CHARSET, DT_CENTER, DT_SINGLELINE, DT_VCENTER, HGDIOBJ, OUT_DEFAULT_PRECIS,
    PAINTSTRUCT, SIMPLEREGION, TRANSPARENT,
};
use windows::Win32::UI::HiDpi::GetDpiForWindow;
use windows::Win32::UI::Shell::{DefSubclassProc, SetWindowSubclass};
use windows::Win32::UI::WindowsAndMessaging::{
    BeginDeferWindowPos, CreateWindowExW, DefWindowProcW, GetWindow, GW_HWNDPREV, DeferWindowPos, DestroyWindow, EndDeferWindowPos,
    GetAncestor, GetClientRect, GetWindowLongPtrW, LoadCursorW, RegisterClassW, SetCursor,
    SetLayeredWindowAttributes, IDC_ARROW, LWA_ALPHA, MA_NOACTIVATEANDEAT, WM_ERASEBKGND, WM_MOUSEACTIVATE, WM_PAINT,
    WM_SETCURSOR, WM_SIZE, WNDCLASSW, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_POPUP,
    GetWindowRect, GetWindowThreadProcessId, IsHungAppWindow, IsIconic, IsWindow, PostMessageW,
    RegisterWindowMessageW, SetWindowLongPtrW, SetWindowPos, ShowWindow, GA_ROOT, GWLP_HWNDPARENT, GWL_EXSTYLE,
    GWL_STYLE, HWND_TOP, SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOOWNERZORDER, SWP_NOSIZE, SWP_NOZORDER,
    SWP_SHOWWINDOW, SW_HIDE, SW_RESTORE, WM_SIZING, WMSZ_BOTTOMLEFT, WMSZ_LEFT, WMSZ_TOP, WMSZ_TOPLEFT,
    WMSZ_TOPRIGHT, WS_EX_APPWINDOW, WS_MAXIMIZEBOX, WS_MINIMIZEBOX, WS_THICKFRAME, WS_VISIBLE,
};

pub const LABEL: &str = "plugin-frame";
const PAGE: &str = "static/plugin-frame.html";
// The page's title bar (top) and control bar (bottom) heights, in logical px.
// The page takes them from plugin_frame_info (titleHeight, controlsHeight), so
// this is the only place they're set.
const TITLE_HEIGHT: f64 = 34.0;
const CONTROLS_HEIGHT: f64 = 36.0;
// Narrowest frame, logical px. Small enough for the smallest editors (bx_meter
// is 211 px wide); the page's compact layout fits both bars in it (a Capture
// icon instead of "Capture", [A] / [B] instead of Auto / Bypass, small knobs).
const MIN_WIDTH: f64 = 200.0;
// The scroll bars' thickness, logical px: the same as plugin-frame.css's
// ::-webkit-scrollbar size (the page draws them; the editor stays clear of them).
const SCROLLBAR: f64 = 12.0;
// How often the editor is checked (gone? resized by the plugin itself?). Short,
// so a plugin's own resize corner drags the frame along: reading another
// process's window rect costs next to nothing.
const POLL: Duration = Duration::from_millis(50);
// Polls after which the frame is shown even if its page never reported loading (3 s).
const SHOW_ANYWAY_AFTER: u32 = 60;
const CLOSED_EVENT: &str = "plugin-frame://closed";
const INFO_EVENT: &str = "plugin-frame://info";
// To the frame's page: the editor's viewport changed (View, or null: no scroll bars).
const VIEW_EVENT: &str = "plugin-frame://view";
// To the main page: Space was pressed in the plugin's editor (the worker keeps it from the
// plugin and posts HOTKEY to the frame, which owns the editor): Play / Stop.
const SPACE_EVENT: &str = "plugin-frame://space";
// The frame window's subclass id (SetWindowSubclass), for WM_SIZING.
const SUBCLASS_ID: usize = 0x7645_7874; // "vExt"

static MAIN: AtomicIsize = AtomicIsize::new(0);
static GENERATION: AtomicU64 = AtomicU64::new(0);
static APP: OnceLock<AppHandle> = OnceLock::new();
static REVEAL: OnceLock<u32> = OnceLock::new();
static HOTKEY: OnceLock<u32> = OnceLock::new();

// The message the worker uncloaks a held editor on (host_worker.REVEAL_MESSAGE:
// same name, so the same id system-wide).
fn reveal_message() -> u32 {
    *REVEAL.get_or_init(|| unsafe { RegisterWindowMessageW(w!("VEXT.RevealEditor")) })
}

// What the worker posts to the frame for a key VEXT keeps from the plugin
// (host_worker.HOTKEY_MESSAGE: same name; wParam = the press's number).
fn hotkey_message() -> u32 {
    *HOTKEY.get_or_init(|| unsafe { RegisterWindowMessageW(w!("VEXT.Hotkey")) })
}

#[derive(Clone, Serialize)]
struct Space {
    seq: u64,
}

// Asks the editor's worker to show it (no-op for an editor that isn't held).
fn post_reveal(editor: isize) {
    if is_window(editor) {
        unsafe {
            let _ = PostMessageW(Some(hwnd(editor)), reveal_message(), WPARAM(0), LPARAM(0));
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Normal,
    Minimize,
    Fullscreen,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
struct Rect {
    x: i32,
    y: i32,
    w: i32,
    h: i32,
}

struct Editor {
    hwnd: isize,
    // As found, given back if the editor outlives its frame.
    style: u32,
    ex_style: u32,
    owner: isize,
    // Its client area's size in the normal mode (the part that shows).
    size: (i32, i32),
    // Had a sizing border: full screen gives it the whole body.
    resizable: bool,
}

struct Frame {
    gen: u64,
    plugin_id: i64,
    name: String,
    window: WebviewWindow,
    hwnd: isize,
    editor: Editor,
    mode: Mode,
    // The frame content's top-left relative to the main client area's (normal and
    // minimized), so it follows the main window when that moves.
    offset: (i32, i32),
    // Full screen: the size the editor settled on (None: not asked yet).
    fit: Option<(i32, i32)>,
    // The page has loaded, so the frame is on screen.
    shown: bool,
    // The editor was put on top of the frame once it showed.
    raised: bool,
    // The editor's style change was applied (SWP_FRAMECHANGED).
    framed: bool,
    // Where the editor's client area was last put, to notice the plugin resizing it.
    placed: Option<Rect>,
    // The editor was told to show (it may have been held off the screen until placed).
    revealed: bool,
    // The plugin is bypassed: a dark, click-blocking overlay covers the editor
    // (overlay: its window, made the first time; 0 = none yet).
    bypassed: bool,
    overlay: isize,
    // How far the editor is scrolled in its viewport (physical px), and the
    // viewport last told to the page (None: the editor fits, no scroll bars).
    scroll: (i32, i32),
    view: Option<View>,
}

// An editor bigger than its body: what the page's scroll bars show. Physical px.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct View {
    // The whole editor.
    content_w: i32,
    content_h: i32,
    // The part of the body it shows in (the body minus the scroll bars).
    view_w: i32,
    view_h: i32,
    // Scrolled this far.
    x: i32,
    y: i32,
    // Which scroll bars there are.
    bar_x: bool,
    bar_y: bool,
}

#[derive(Default)]
pub struct Frames {
    frame: Mutex<Option<Frame>>,
    // Serializes plugin_frame_open: two creations would collide on the label.
    open: tauri::async_runtime::Mutex<()>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FrameInfo {
    plugin_id: i64,
    name: String,
    mode: Mode,
    title_height: f64,
    controls_height: f64,
    scrollbar: f64,
    view: Option<View>,
    resizable: bool,
    // The frame content's (client area's) top-left on screen, physical px:
    // where a drag starts from, in plugin_frame_move's coordinates.
    x: i32,
    y: i32,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Closed {
    plugin_id: i64,
}

#[derive(Clone, Serialize)]
pub struct Toggled {
    mode: Mode,
}

// ---------- Win32 helpers ----------

fn hwnd(raw: isize) -> HWND {
    HWND(raw as *mut std::ffi::c_void)
}

fn is_window(raw: isize) -> bool {
    raw != 0 && unsafe { IsWindow(Some(hwnd(raw))).as_bool() }
}

fn window_rect(raw: isize) -> Option<Rect> {
    let mut r = RECT::default();
    unsafe { GetWindowRect(hwnd(raw), &mut r) }.ok()?;
    Some(Rect { x: r.left, y: r.top, w: r.right - r.left, h: r.bottom - r.top })
}

// A window's client area on screen.
fn client_rect(raw: isize) -> Option<Rect> {
    unsafe {
        let mut r = RECT::default();
        GetClientRect(hwnd(raw), &mut r).ok()?;
        let mut origin = POINT::default();
        if !ClientToScreen(hwnd(raw), &mut origin).as_bool() {
            return None;
        }
        Some(Rect { x: origin.x, y: origin.y, w: r.right, h: r.bottom })
    }
}

// The main window's client area on screen; None while it's minimized.
fn main_client() -> Option<Rect> {
    let raw = MAIN.load(Ordering::Relaxed);
    if raw == 0 || unsafe { IsIconic(hwnd(raw)).as_bool() } {
        return None;
    }
    client_rect(raw).filter(|r| r.w > 0 && r.h > 0)
}

// The frame's invisible borders (left, top, right, bottom): tao keeps
// resize-border insets around an undecorated window that has a shadow
// (8/1/8/8 px at 96 DPI). The layout places the frame's client area, which is
// what its page draws, and grows the window rect by these.
fn insets(raw: isize) -> (i32, i32, i32, i32) {
    match (window_rect(raw), client_rect(raw)) {
        (Some(w), Some(c)) => (c.x - w.x, c.y - w.y, (w.x + w.w) - (c.x + c.w), (w.y + w.h) - (c.y + c.h)),
        _ => (0, 0, 0, 0),
    }
}

fn scale(raw: isize) -> f64 {
    match unsafe { GetDpiForWindow(hwnd(raw)) } {
        0 => 1.0,
        dpi => dpi as f64 / 96.0,
    }
}

fn clamp(c: Rect, x: i32, y: i32, w: i32, h: i32) -> (i32, i32) {
    (x.min(c.x + c.w - w).max(c.x), y.min(c.y + c.h - h).max(c.y))
}

// The part two rects share (None: nothing).
fn intersect(a: Rect, b: Rect) -> Option<Rect> {
    let (x0, y0) = (a.x.max(b.x), a.y.max(b.y));
    let (x1, y1) = ((a.x + a.w).min(b.x + b.w), (a.y + a.h).min(b.y + b.h));
    (x1 > x0 && y1 > y0).then_some(Rect { x: x0, y: y0, w: x1 - x0, h: y1 - y0 })
}

// A top-level window of another process: the main page can only hand over a
// plugin editor, never one of VEXT's own windows.
fn check_editor(raw: isize) -> Result<(), String> {
    if !is_window(raw) {
        return Err("the plugin's window is gone".into());
    }
    unsafe {
        let h = hwnd(raw);
        let mut pid = 0u32;
        GetWindowThreadProcessId(h, Some(&mut pid));
        if pid == std::process::id() || GetAncestor(h, GA_ROOT) != h {
            return Err("that is not a plugin window".into());
        }
    }
    Ok(())
}

impl Editor {
    // Takes the editor into the frame: owned by it, out of the taskbar, no
    // minimize/maximize boxes. Nothing that changes its geometry: its caption
    // and borders stay, and clip() hides them. Stripping the caption (as
    // before) left resizable JUCE editors with a grey band: JUCE measures its
    // window's border only when it resizes the window itself, so it went on
    // subtracting the old caption and borders from every size it was given
    // (iZotope Insight 2: 886x611 of plugin in a 902x650 window).
    unsafe fn adopt(raw: isize, frame: isize, fallback: (i32, i32)) -> Result<Editor, String> {
        let h = hwnd(raw);
        if IsIconic(h).as_bool() {
            let _ = ShowWindow(h, SW_RESTORE);
        }
        let mut client = RECT::default();
        let size = match GetClientRect(h, &mut client) {
            Ok(()) if client.right > 0 && client.bottom > 0 => (client.right, client.bottom),
            _ => fallback,
        };
        if size.0 <= 0 || size.1 <= 0 {
            return Err("the plugin's window has no size yet".into());
        }
        let style = GetWindowLongPtrW(h, GWL_STYLE) as u32;
        let ex_style = GetWindowLongPtrW(h, GWL_EXSTYLE) as u32;
        let owner = GetWindowLongPtrW(h, GWLP_HWNDPARENT);
        SetWindowLongPtrW(h, GWLP_HWNDPARENT, frame);
        // Applied by the first layout (SWP_FRAMECHANGED); neither changes the client area.
        SetWindowLongPtrW(h, GWL_STYLE, (style & !(WS_MINIMIZEBOX | WS_MAXIMIZEBOX).0) as isize);
        SetWindowLongPtrW(h, GWL_EXSTYLE, (ex_style & !WS_EX_APPWINDOW.0) as isize);
        // A region only clips the caption once DWM no longer draws it (with
        // DWM's frame the region was dropped or ignored); clip() sets it next.
        set_nc_rendering(h, DWMNCRP_DISABLED);
        Ok(Editor { hwnd: raw, style, ex_style, owner, size, resizable: style & WS_THICKFRAME.0 != 0 })
    }

    // Gives a still-open editor its own window back (owner, region, boxes),
    // where it is. Nothing to do once it's gone.
    unsafe fn release(&self) {
        if !is_window(self.hwnd) {
            return;
        }
        post_reveal(self.hwnd); // never left held off the screen
        let h = hwnd(self.hwnd);
        let _ = SetWindowRgn(h, None, true);
        set_nc_rendering(h, DWMNCRP_USEWINDOWSTYLE);
        let visible = GetWindowLongPtrW(h, GWL_STYLE) as u32 & WS_VISIBLE.0;
        SetWindowLongPtrW(h, GWL_STYLE, ((self.style & !WS_VISIBLE.0) | visible) as isize);
        SetWindowLongPtrW(h, GWL_EXSTYLE, self.ex_style as isize);
        SetWindowLongPtrW(h, GWLP_HWNDPARENT, self.owner);
        let _ = SetWindowPos(
            h,
            None,
            0,
            0,
            0,
            0,
            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        );
    }
}

// Whether DWM draws the window's caption and borders (works across processes).
unsafe fn set_nc_rendering(h: HWND, policy: DWMNCRENDERINGPOLICY) {
    let _ = DwmSetWindowAttribute(
        h,
        DWMWA_NCRENDERING_POLICY,
        &policy as *const DWMNCRENDERINGPOLICY as *const std::ffi::c_void,
        std::mem::size_of::<DWMNCRENDERINGPOLICY>() as u32,
    );
}

// Where a window must go for its client area to be at `client`: the client
// rect grown by the window's caption and borders.
fn window_for_client(raw: isize, client: Rect) -> Rect {
    let (l, t, r, b) = insets(raw);
    Rect { x: client.x - l, y: client.y - t, w: client.w + l + r, h: client.h + t + b }
}

// Shows only the editor's client area, and of that only what is inside
// `view` (screen coordinates: the body's viewport, smaller than the editor
// when it scrolls): its window region (window coordinates) is set to that,
// unless it already is. Called after every placement, so it follows the
// plugin resizing its window and the scrolling.
unsafe fn clip(raw: isize, view: Rect) {
    let (Some(w), Some(c)) = (window_rect(raw), client_rect(raw)) else { return };
    let shown = intersect(c, view).unwrap_or(Rect { x: c.x, y: c.y, w: 0, h: 0 });
    let (left, top) = (shown.x - w.x, shown.y - w.y);
    let want = RECT { left, top, right: left + shown.w, bottom: top + shown.h };
    let h = hwnd(raw);
    let mut have = RECT::default();
    if GetWindowRgnBox(h, &mut have) == SIMPLEREGION
        && (have.left, have.top, have.right, have.bottom) == (want.left, want.top, want.right, want.bottom)
    {
        return;
    }
    let region = CreateRectRgn(want.left, want.top, want.right, want.bottom);
    if region.is_invalid() {
        return;
    }
    // On success the system owns the region; otherwise it's still ours.
    if SetWindowRgn(h, Some(region), true) == 0 {
        let _ = DeleteObject(HGDIOBJ(region.0));
    }
}

// ---------- layout ----------

struct Plan {
    gen: u64,
    frame: isize,
    editor: isize,
    frame_rect: Rect,
    // Where the editor's client area goes (all of it, also when only part of
    // it shows). None: the editor is hidden (minimized).
    editor_rect: Option<Rect>,
    // The body's part the editor shows in (screen): the editor is clipped to it.
    viewport: Rect,
    // The scroll position kept in range, and what the page's scroll bars show.
    scroll: (i32, i32),
    view: Option<View>,
    show: bool,
    raise: bool,
    frame_changed: bool,
    // The frame shows and the editor is placed: tell a held editor to show.
    reveal: bool,
    // The bypass overlay (0 = none) and whether it shows (over the editor's visible part).
    overlay: isize,
    overlay_on: bool,
}

// The frame's fixed parts, physical px at the frame's DPI.
#[derive(Clone, Copy)]
struct Chrome {
    // The title bar, above the editor.
    title: i32,
    // The control bar, below the editor.
    controls: i32,
    // The narrowest frame.
    min_w: i32,
    // A scroll bar's thickness.
    sb: i32,
}

impl Chrome {
    // Both bars together: the frame's height minus the editor's.
    fn bars(self) -> i32 {
        self.title + self.controls
    }
}

fn chrome(f: &Frame) -> Chrome {
    let s = scale(f.hwnd);
    Chrome {
        title: (TITLE_HEIGHT * s).round() as i32,
        controls: (CONTROLS_HEIGHT * s).round() as i32,
        min_w: (MIN_WIDTH * s).round() as i32,
        sb: (SCROLLBAR * s).round() as i32,
    }
}

// The body for an editor of `ew` x `eh` when the body may be at most `max_w`
// x `max_h` (and at least `min_w` wide): the editor's own size, or the most
// there is with scroll bars where it doesn't fit (a vertical bar takes width,
// which can make a horizontal one needed too, and the other way round).
#[derive(Clone, Copy)]
struct Body {
    w: i32,
    h: i32,
    bar_x: bool,
    bar_y: bool,
}

impl Body {
    // The viewport's size: the body minus its scroll bars.
    fn view(self, sb: i32) -> (i32, i32) {
        (self.w - if self.bar_y { sb } else { 0 }, self.h - if self.bar_x { sb } else { 0 })
    }
}

fn body(ew: i32, eh: i32, max_w: i32, max_h: i32, min_w: i32, sb: i32) -> Body {
    let (max_w, max_h) = (max_w.max(sb + 1), max_h.max(sb + 1));
    let mut bar_y = eh > max_h;
    let mut bar_x = ew > max_w;
    if bar_y && !bar_x && ew + sb > max_w {
        bar_x = true;
    }
    if bar_x && !bar_y && eh + sb > max_h {
        bar_y = true;
    }
    let w = if bar_x { max_w } else { (ew + if bar_y { sb } else { 0 }).max(min_w).min(max_w) };
    let h = if bar_y { max_h } else { eh + if bar_x { sb } else { 0 } };
    Body { w, h, bar_x, bar_y }
}

// The frame's content size (its client area: title bar + body + control bar)
// in the normal and minimized modes, at most the main window's client area
// `client`. Minimized keeps both bars (the knobs, Auto and Bypass still work)
// and drops the body.
fn frame_size(f: &Frame, client: Rect) -> (i32, i32) {
    let c = chrome(f);
    let (ew, eh) = f.editor.size;
    let b = body(ew, eh, client.w, client.h - c.bars(), c.min_w, c.sb);
    (b.w, if f.mode == Mode::Minimize { c.bars() } else { c.bars() + b.h })
}

// `client`: the main window's client area. The frame's content is placed
// inside it; its window rect adds the invisible borders around that.
fn plan(f: &Frame, client: Rect) -> Plan {
    let c = chrome(f);
    let title = c.title;
    let max_h = (client.h - c.bars()).max(1);
    // The frame's content rect, the editor's size and the body it goes on.
    let (content, (ew, eh), b) = match f.mode {
        Mode::Fullscreen => {
            // The whole client area; the editor centered on the body (between
            // the two bars), as big as the body if it can be resized; scroll
            // bars if it still doesn't fit.
            let size = f.fit.unwrap_or(if f.editor.resizable { (client.w, max_h) } else { f.editor.size });
            let b = body(size.0, size.1, client.w, max_h, client.w, c.sb);
            (client, size, Body { w: client.w, h: max_h, ..b })
        }
        _ => {
            let (ew, eh) = f.editor.size;
            let b = body(ew, eh, client.w, max_h, c.min_w, c.sb);
            let (w, h) = (b.w, if f.mode == Mode::Minimize { c.bars() } else { c.bars() + b.h });
            let (x, y) = clamp(client, client.x + f.offset.0, client.y + f.offset.1, w, h);
            (Rect { x, y, w, h }, (ew, eh), b)
        }
    };
    let (view_w, view_h) = b.view(c.sb);
    let viewport = Rect { x: content.x, y: content.y + title, w: view_w, h: view_h };
    let scroll = ((f.scroll.0).clamp(0, (ew - view_w).max(0)), (f.scroll.1).clamp(0, (eh - view_h).max(0)));
    let shows = f.mode != Mode::Minimize;
    // Centered where it's smaller than the viewport.
    let editor_rect = shows.then_some(Rect {
        x: viewport.x + ((view_w - ew) / 2).max(0) - scroll.0,
        y: viewport.y + ((view_h - eh) / 2).max(0) - scroll.1,
        w: ew,
        h: eh,
    });
    let view = (shows && (b.bar_x || b.bar_y)).then_some(View {
        content_w: ew,
        content_h: eh,
        view_w,
        view_h,
        x: scroll.0,
        y: scroll.1,
        bar_x: b.bar_x,
        bar_y: b.bar_y,
    });
    let (l, t, r, b) = insets(f.hwnd);
    Plan {
        gen: f.gen,
        frame: f.hwnd,
        editor: f.editor.hwnd,
        frame_rect: Rect { x: content.x - l, y: content.y - t, w: content.w + l + r, h: content.h + t + b },
        editor_rect,
        viewport,
        scroll,
        view,
        show: f.shown,
        raise: f.shown && !f.raised,
        frame_changed: !f.framed,
        reveal: f.shown && !f.revealed && editor_rect.is_some(),
        overlay: f.overlay,
        overlay_on: f.bypassed && f.shown && editor_rect.is_some(),
    }
}

// Moves frame and editor together. Returns where the editor's client area
// ended up.
fn apply(p: &Plan) -> Option<Rect> {
    unsafe {
        let frame = hwnd(p.frame);
        let editor = hwnd(p.editor);
        // Moving a window of another process waits for that process: a
        // plugin that stopped answering is left where it is.
        let editor_ok = is_window(p.editor) && !IsHungAppWindow(editor).as_bool();
        let base = SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER;
        let frame_flags = if p.show { base | SWP_SHOWWINDOW } else { base };
        let mut editor_flags = base | SWP_SHOWWINDOW;
        if p.frame_changed {
            editor_flags |= SWP_FRAMECHANGED;
        }
        let editor_rect = p.editor_rect.filter(|_| editor_ok).map(|c| window_for_client(p.editor, c));
        let f = p.frame_rect;
        let deferred = (|| -> windows::core::Result<()> {
            let mut dwp = BeginDeferWindowPos(2)?;
            dwp = DeferWindowPos(dwp, frame, None, f.x, f.y, f.w, f.h, frame_flags)?;
            if let Some(e) = editor_rect {
                dwp = DeferWindowPos(dwp, editor, None, e.x, e.y, e.w, e.h, editor_flags)?;
            }
            EndDeferWindowPos(dwp)
        })();
        if deferred.is_err() {
            let _ = SetWindowPos(frame, None, f.x, f.y, f.w, f.h, frame_flags);
            if let Some(e) = editor_rect {
                let _ = SetWindowPos(editor, None, e.x, e.y, e.w, e.h, editor_flags);
            }
        }
        if !editor_ok {
            return None;
        }
        if p.editor_rect.is_none() {
            let _ = ShowWindow(editor, SW_HIDE);
        } else {
            clip(p.editor, p.viewport);
            if p.raise {
                let _ = SetWindowPos(frame, Some(HWND_TOP), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
                let _ = SetWindowPos(editor, Some(HWND_TOP), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            }
            if p.reveal {
                // In place, clipped, above the frame that is now on screen: the worker uncloaks it.
                post_reveal(p.editor);
            }
        }
        let placed = client_rect(p.editor);
        if is_window(p.overlay) {
            match placed.and_then(|r| intersect(r, p.viewport)).filter(|_| p.overlay_on) {
                Some(r) => {
                    // Right above the editor: inserted after the window above it
                    // (HWND_TOP left it under its owner, the editor of another process).
                    let above = GetWindow(editor, GW_HWNDPREV).ok().filter(|h| !h.is_invalid());
                    let (after, z) = match above {
                        Some(h) if h.0 as isize == p.overlay => (None, SWP_NOZORDER),
                        Some(h) => (Some(h), SWP_NOACTIVATE),
                        None => (Some(HWND_TOP), SWP_NOACTIVATE),
                    };
                    let _ = SetWindowPos(
                        hwnd(p.overlay),
                        after,
                        r.x,
                        r.y,
                        r.w,
                        r.h,
                        SWP_NOACTIVATE | SWP_SHOWWINDOW | z,
                    );
                }
                None => {
                    let _ = ShowWindow(hwnd(p.overlay), SW_HIDE);
                }
            }
        }
        placed
    }
}

fn with_frame<T>(app: &AppHandle, f: impl FnOnce(&mut Frame) -> T) -> Option<T> {
    let frames = app.state::<Frames>();
    let mut guard = frames.frame.lock().unwrap();
    guard.as_mut().map(f)
}

// Puts frame and editor where the current mode wants them (main thread).
// A plugin that keeps a size of its own gets the frame fitted around it.
fn relayout(app: &AppHandle) {
    for _ in 0..3 {
        let Some(client) = main_client() else { return };
        let Some(p) = with_frame(app, |f| plan(f, client)) else { return };
        let placed = apply(&p);
        let mut told = None;
        let again = with_frame(app, |f| {
            if f.gen != p.gen {
                return false;
            }
            f.framed = true;
            f.raised |= p.raise;
            f.revealed |= p.reveal && placed.is_some();
            f.placed = placed;
            f.scroll = p.scroll;
            if f.view != p.view {
                f.view = p.view;
                told = Some(p.view);
            }
            match (p.editor_rect, placed) {
                (Some(want), Some(got)) if (got.w, got.h) != (want.w, want.h) => {
                    if f.mode == Mode::Fullscreen {
                        f.fit = Some((got.w, got.h));
                    } else {
                        f.editor.size = (got.w, got.h);
                    }
                    true
                }
                _ => false,
            }
        });
        if let Some(view) = told {
            let _ = app.emit_to(LABEL, VIEW_EVENT, view);
        }
        if again != Some(true) {
            return;
        }
    }
}

// ---------- the bypass overlay ----------

const OVERLAY_ALPHA: u8 = 168; // of 255: the editor stays visible underneath, darkened
// The palette's Black (#0a0908) and White Smoke (#f2f4f3), as COLORREF (0x00BBGGRR).
const OVERLAY_BG: u32 = 0x0008_090a;
const OVERLAY_FG: u32 = 0x00f3_f4f2;

unsafe extern "system" fn overlay_proc(h: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_PAINT => {
            let mut ps = PAINTSTRUCT::default();
            let dc = BeginPaint(h, &mut ps);
            let mut r = RECT::default();
            let _ = GetClientRect(h, &mut r);
            let brush = CreateSolidBrush(COLORREF(OVERLAY_BG));
            FillRect(dc, &r, brush);
            let _ = DeleteObject(HGDIOBJ(brush.0));
            let dpi = GetDpiForWindow(h).max(96) as i32;
            let font = CreateFontW(
                -(22 * dpi / 96),
                0,
                0,
                0,
                600,
                0,
                0,
                0,
                DEFAULT_CHARSET,
                OUT_DEFAULT_PRECIS,
                CLIP_DEFAULT_PRECIS,
                CLEARTYPE_QUALITY,
                0,
                w!("Segoe UI"),
            );
            let old = SelectObject(dc, HGDIOBJ(font.0));
            SetBkMode(dc, TRANSPARENT);
            SetTextColor(dc, COLORREF(OVERLAY_FG));
            let mut text: Vec<u16> = "Bypassed".encode_utf16().collect();
            DrawTextW(dc, &mut text, &mut r, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
            SelectObject(dc, old);
            let _ = DeleteObject(HGDIOBJ(font.0));
            let _ = EndPaint(h, &ps);
            LRESULT(0)
        }
        WM_ERASEBKGND => LRESULT(1),
        // Takes clicks without activating anything: the plugin is out of play while bypassed.
        WM_MOUSEACTIVATE => LRESULT(MA_NOACTIVATEANDEAT as isize),
        WM_SETCURSOR => {
            if let Ok(c) = LoadCursorW(None, IDC_ARROW) {
                SetCursor(Some(c));
            }
            LRESULT(1)
        }
        WM_SIZE => {
            let _ = InvalidateRect(Some(h), None, true);
            DefWindowProcW(h, msg, wparam, lparam)
        }
        _ => DefWindowProcW(h, msg, wparam, lparam),
    }
}

// A hidden overlay window owned by the editor (owned windows stay above their
// owner), main thread. 0 if it couldn't be made.
fn create_overlay(editor: isize) -> isize {
    static CLASS: OnceLock<bool> = OnceLock::new();
    unsafe {
        let class = w!("VEXTBypassOverlay");
        let registered = *CLASS.get_or_init(|| {
            let wc = WNDCLASSW { lpfnWndProc: Some(overlay_proc), lpszClassName: class, ..Default::default() };
            RegisterClassW(&wc) != 0
        });
        if !registered || !is_window(editor) {
            return 0;
        }
        match CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            class,
            w!("Bypassed"),
            WS_POPUP,
            0,
            0,
            1,
            1,
            Some(hwnd(editor)),
            None,
            None,
            None,
        ) {
            Ok(h) => {
                let _ = SetLayeredWindowAttributes(h, COLORREF(0), OVERLAY_ALPHA, LWA_ALPHA);
                h.0 as isize
            }
            Err(_) => 0,
        }
    }
}

fn destroy_overlay(overlay: isize) {
    if is_window(overlay) {
        unsafe {
            let _ = DestroyWindow(hwnd(overlay));
        }
    }
}

// ---------- resizing by the frame's edges ----------

// The frame is resizable (its invisible borders) only for a resizable editor in
// the normal mode (main thread).
fn sync_resizable(f: &Frame) {
    let _ = f.window.set_resizable(f.editor.resizable && f.mode == Mode::Normal);
}

// WM_SIZING on the frame (main thread): `r` is the window rect the user's drag
// proposes. The editor is resized to fit it (within the main window), and `r`
// is changed to fit what the plugin accepted, anchored on the dragged edge.
// Returns whether `r` was handled.
fn on_sizing(app: &AppHandle, edge: u32, r: &mut RECT) -> bool {
    let Some(client) = main_client() else { return false };
    let Some((frame, editor, c)) = with_frame(app, |f| {
        (f.mode == Mode::Normal && f.editor.resizable).then(|| (f.hwnd, f.editor.hwnd, chrome(f)))
    })
    .flatten() else {
        return false;
    };
    if !is_window(editor) || unsafe { IsHungAppWindow(hwnd(editor)).as_bool() } {
        return false;
    }
    let (l, t, rr, b) = insets(frame);
    // The proposed content (client) rect, then the editor's share of it (the
    // body, between the title bar and the control bar).
    let content = Rect { x: r.left + l, y: r.top + t, w: r.right - r.left - l - rr, h: r.bottom - r.top - t - b };
    let want_w = content.w.clamp(1, client.w);
    let want_h = (content.h - c.bars()).clamp(1, (client.h - c.bars()).max(1));
    let target = window_for_client(editor, Rect { x: content.x, y: content.y + c.title, w: want_w, h: want_h });
    unsafe {
        let _ = SetWindowPos(
            hwnd(editor),
            None,
            target.x,
            target.y,
            target.w,
            target.h,
            SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER,
        );
    }
    // What the plugin settled on (its minimum, maximum, steps or aspect ratio).
    let Some(got) = client_rect(editor) else { return false };
    let new_w = got.w.max(c.min_w) + l + rr;
    let new_h = c.bars() + got.h + t + b;
    let from_left = matches!(edge, WMSZ_LEFT | WMSZ_TOPLEFT | WMSZ_BOTTOMLEFT);
    let from_top = matches!(edge, WMSZ_TOP | WMSZ_TOPLEFT | WMSZ_TOPRIGHT);
    if from_left {
        r.left = r.right - new_w;
    } else {
        r.right = r.left + new_w;
    }
    if from_top {
        r.top = r.bottom - new_h;
    } else {
        r.bottom = r.top + new_h;
    }
    let _ = with_frame(app, |f| {
        f.editor.size = (got.w, got.h);
        f.offset = (r.left + l - client.x, r.top + t - client.y);
    });
    true
}

// The frame window's procedure in front of tao's (SetWindowSubclass; main thread).
unsafe extern "system" fn frame_subclass(
    h: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    _id: usize,
    _data: usize,
) -> LRESULT {
    if msg == hotkey_message() {
        if let Some(app) = APP.get() {
            let _ = app.emit_to("main", SPACE_EVENT, Space { seq: wparam.0 as u64 });
        }
        return LRESULT(0);
    }
    if msg == WM_SIZING && lparam.0 != 0 {
        if let Some(app) = APP.get() {
            if on_sizing(app, wparam.0 as u32, &mut *(lparam.0 as *mut RECT)) {
                return LRESULT(1);
            }
        }
    }
    DefSubclassProc(h, msg, wparam, lparam)
}

// Runs `f` on the main thread and hands its result back to the command.
async fn on_main<T: Send + 'static>(
    app: &AppHandle,
    f: impl FnOnce(&AppHandle) -> T + Send + 'static,
) -> Result<T, String> {
    let (tx, mut rx) = tauri::async_runtime::channel(1);
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let _ = tx.try_send(f(&handle));
    })
    .map_err(|e| e.to_string())?;
    rx.recv().await.ok_or_else(|| "VEXT is closing".to_string())
}

fn info(f: &Frame) -> FrameInfo {
    let at = client_rect(f.hwnd).unwrap_or_default();
    FrameInfo {
        plugin_id: f.plugin_id,
        name: f.name.clone(),
        mode: f.mode,
        title_height: TITLE_HEIGHT,
        controls_height: CONTROLS_HEIGHT,
        scrollbar: SCROLLBAR,
        view: f.view,
        resizable: f.editor.resizable,
        x: at.x,
        y: at.y,
    }
}

fn announce(app: &AppHandle) {
    if let Some(info) = with_frame(app, |f| info(f)) {
        let _ = app.emit_to(LABEL, INFO_EVENT, info);
    }
}

// Closes the frame (with `gen`: only if it's still that one), giving a
// still-open editor its own window back, and tells the main page.
fn close(app: &AppHandle, gen: Option<u64>) {
    let frame = {
        let frames = app.state::<Frames>();
        let mut guard = frames.frame.lock().unwrap();
        if gen.is_some_and(|g| guard.as_ref().map(|f| f.gen) != Some(g)) {
            return;
        }
        guard.take()
    };
    let Some(f) = frame else { return };
    destroy_overlay(f.overlay);
    unsafe { f.editor.release() };
    let _ = f.window.destroy();
    let _ = app.emit_to("main", CLOSED_EVENT, Closed { plugin_id: f.plugin_id });
}

// Watches one editor (one frame generation): closes the frame when the editor
// window disappears, and refits it when the plugin resizes its editor.
fn watch(app: AppHandle, gen: u64, editor: isize) {
    std::thread::spawn(move || {
        let mut ticks = 0u32;
        loop {
            std::thread::sleep(POLL);
            ticks = ticks.saturating_add(1);
            let Some((placed, shown, mode)) = with_frame(&app, |f| (f.gen == gen).then_some((f.placed, f.shown, f.mode)))
                .flatten()
            else {
                return; // closed, or another editor took the frame
            };
            if !is_window(editor) {
                let a = app.clone();
                let _ = app.run_on_main_thread(move || close(&a, Some(gen)));
                return;
            }
            // Its client area: `placed` is where that was put.
            let now = client_rect(editor);
            let changed = mode != Mode::Minimize && placed.is_some() && now != placed && main_client().is_some();
            let late = !shown && ticks == SHOW_ANYWAY_AFTER;
            if changed || late {
                let a = app.clone();
                let _ = app.run_on_main_thread(move || {
                    let ok = with_frame(&a, |f| {
                        if f.gen != gen {
                            return false;
                        }
                        f.shown |= late;
                        if let Some(r) = client_rect(editor).filter(|_| changed) {
                            if f.mode == Mode::Fullscreen {
                                f.fit = Some((r.w, r.h));
                            } else if f.mode == Mode::Normal {
                                f.editor.size = (r.w, r.h);
                            }
                        }
                        true
                    });
                    if ok == Some(true) {
                        relayout(&a);
                    }
                });
            }
        }
    });
}

fn page_loaded(app: &AppHandle) {
    let a = app.clone();
    let _ = app.run_on_main_thread(move || {
        let first = with_frame(&a, |f| !std::mem::replace(&mut f.shown, true));
        if first == Some(true) {
            relayout(&a);
        }
    });
}

fn create_window(app: &AppHandle, plugin_id: i64, name: &str) -> Result<WebviewWindow, String> {
    // The main window by its handle: once it holds the browser webview too it
    // is no longer a single-webview window, so get_webview_window("main")
    // finds nothing (and owner() needs one).
    let main = MAIN.load(Ordering::Relaxed);
    if !is_window(main) {
        return Err("the main window is gone".into());
    }
    // id and name in the address too, so the title bar has them on its first paint.
    let query = Url::parse_with_params("http://x/", &[("id", plugin_id.to_string()), ("name", name.to_string())])
        .ok()
        .and_then(|u| u.query().map(str::to_string))
        .unwrap_or_default();
    WebviewWindowBuilder::new(app, LABEL, WebviewUrl::App(format!("{PAGE}?{query}").into()))
        .title(format!("{name} - VEXT"))
        .decorations(false)
        .shadow(true)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .skip_taskbar(true)
        .visible(false)
        .focused(false)
        .inner_size(MIN_WIDTH, TITLE_HEIGHT + CONTROLS_HEIGHT)
        .owner_raw(hwnd(main))
        .initialization_script(crate::config().init_script())
        // Identical to every other webview's (see crate::WEBVIEW_ARGS): one WebView2 environment.
        .additional_browser_args(crate::WEBVIEW_ARGS)
        // Only its own page: no navigating away, no new windows.
        .on_navigation(|url| url.host_str() == Some("tauri.localhost"))
        .on_new_window(|_, _| NewWindowResponse::Deny)
        .on_page_load(|w, payload| {
            if payload.event() == PageLoadEvent::Finished {
                page_loaded(w.app_handle());
            }
        })
        .build()
        .map_err(|e| e.to_string())
}

// Hands the editor to the frame window (main thread).
fn attach(
    app: &AppHandle,
    window: WebviewWindow,
    frame_hwnd: isize,
    plugin_id: i64,
    name: String,
    editor: isize,
    fallback: (i32, i32),
) -> Result<(), String> {
    check_editor(editor)?;
    let old = {
        let frames = app.state::<Frames>();
        let mut guard = frames.frame.lock().unwrap();
        guard.take()
    };
    let reused = old.is_some();
    if reused {
        let _ = window.set_title(&format!("{name} - VEXT"));
    }
    let gen = GENERATION.fetch_add(1, Ordering::Relaxed) + 1;
    let frame = match old {
        // The same editor again (the page re-sent it): nothing changes but
        // the name and the watcher.
        Some(o) if o.editor.hwnd == editor => Frame { gen, plugin_id, name, placed: None, view: None, ..o },
        o => {
            if let Some(o) = &o {
                destroy_overlay(o.overlay);
                unsafe { o.editor.release() };
            }
            let taken = match unsafe { Editor::adopt(editor, frame_hwnd, fallback) } {
                Ok(e) => e,
                Err(err) => {
                    if reused {
                        // The old editor is out and the new one can't come in.
                        let _ = window.destroy();
                    }
                    return Err(err);
                }
            };
            let mut frame = Frame {
                gen,
                plugin_id,
                name,
                window,
                hwnd: frame_hwnd,
                editor: taken,
                mode: Mode::Normal,
                offset: (0, 0),
                fit: None,
                shown: false,
                raised: false,
                framed: false,
                placed: None,
                revealed: false,
                bypassed: false,
                overlay: 0,
                scroll: (0, 0),
                view: None,
            };
            match o {
                // Another plugin in the same frame: same place, normal mode.
                Some(o) => {
                    frame.offset = o.offset;
                    frame.shown = o.shown;
                }
                // A new frame starts centered on the main window.
                None => {
                    frame.offset = main_client().map_or((40, 40), |c| {
                        let (w, h) = frame_size(&frame, c);
                        (((c.w - w) / 2).max(0), ((c.h - h) / 2).max(0))
                    });
                }
            }
            frame
        }
    };
    // WM_SIZING for resizing by the frame's edges (installed once per frame window;
    // only the window's own thread may subclass it: this is the main thread).
    unsafe {
        let _ = SetWindowSubclass(hwnd(frame_hwnd), Some(frame_subclass), SUBCLASS_ID, 0);
    }
    sync_resizable(&frame);
    *app.state::<Frames>().frame.lock().unwrap() = Some(frame);
    relayout(app);
    if reused {
        announce(app);
    }
    watch(app.clone(), gen, editor);
    Ok(())
}

// ---------- commands ----------

#[tauri::command]
pub async fn plugin_frame_open(
    app: AppHandle,
    plugin_id: i64,
    name: String,
    hwnd: i64,
    width: Option<f64>,
    height: Option<f64>,
) -> Result<(), String> {
    let frames = app.state::<Frames>();
    let _serial = frames.open.lock().await;
    let editor = hwnd as isize;
    check_editor(editor)?;
    let existing = with_frame(&app, |f| f.window.clone());
    let window = match existing {
        Some(w) => w,
        None => {
            // A frame closed a moment ago may still be on its way out.
            for _ in 0..40 {
                if app.get_webview_window(LABEL).is_none() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            create_window(&app, plugin_id, &name)?
        }
    };
    let frame_hwnd = window.hwnd().map_err(|e| e.to_string())?.0 as isize;
    let fallback = (width.unwrap_or(0.0).round() as i32, height.unwrap_or(0.0).round() as i32);
    let result = on_main(&app, move |app| attach(app, window, frame_hwnd, plugin_id, name, editor, fallback)).await?;
    if result.is_err() && with_frame(&app, |_| ()).is_none() {
        // Nothing went into a new window: don't leave it behind.
        if let Some(w) = app.get_webview_window(LABEL) {
            let _ = w.destroy();
        }
    }
    result
}

#[tauri::command]
pub async fn plugin_frame_close(app: AppHandle) -> Result<(), String> {
    on_main(&app, |app| close(app, None)).await
}

// x, y: where the frame content's (client area's) top-left should go, on
// screen, physical px (the page's screenX deltas times devicePixelRatio). Kept
// inside the main window's client area.
#[tauri::command]
pub async fn plugin_frame_move(app: AppHandle, x: f64, y: f64) -> Result<(), String> {
    on_main(&app, move |app| {
        let Some(client) = main_client() else { return };
        let moved = with_frame(app, |f| {
            if f.mode == Mode::Fullscreen {
                return false;
            }
            let (w, h) = frame_size(f, client);
            // Clamped here, so a drag past an edge comes back from the edge.
            let (cx, cy) = clamp(client, x.round() as i32, y.round() as i32, w, h);
            f.offset = (cx - client.x, cy - client.y);
            true
        });
        if moved == Some(true) {
            relayout(app);
        }
    })
    .await
}

// 'minimize' and 'fullscreen' toggle (again = back to normal); 'restore' is
// normal. Returns the mode now: 'normal' | 'minimize' | 'fullscreen'.
#[tauri::command]
pub async fn plugin_frame_toggle(app: AppHandle, mode: String) -> Result<Toggled, String> {
    let wanted = match mode.as_str() {
        "minimize" => Mode::Minimize,
        "fullscreen" => Mode::Fullscreen,
        "restore" | "normal" => Mode::Normal,
        _ => return Err(format!("unknown frame mode: {mode}")),
    };
    let now = on_main(&app, move |app| {
        let (now, window, resizable) = with_frame(app, |f| {
            f.mode = if f.mode == wanted { Mode::Normal } else { wanted };
            f.fit = None;
            f.scroll = (0, 0);
            (f.mode, f.window.clone(), f.editor.resizable && f.mode == Mode::Normal)
        })?;
        // Outside the frame lock: changing the style can send window events (relayout).
        let _ = window.set_resizable(resizable);
        relayout(app);
        announce(app);
        Some(now)
    })
    .await?;
    now.map(|mode| Toggled { mode }).ok_or_else(|| "no plugin is open".to_string())
}

// Bypass on: a dark overlay over the editor's visible part that says so and
// takes the clicks (the plugin is out of the audio path); off: the editor
// again. Called by the frame's page: its B button, and its poll when the
// host's bypass changed otherwise.
#[tauri::command]
pub async fn plugin_frame_bypass(app: AppHandle, on: bool) -> Result<(), String> {
    on_main(&app, move |app| {
        let Some((editor, overlay)) = with_frame(app, |f| {
            f.bypassed = on;
            (f.editor.hwnd, f.overlay)
        }) else {
            return;
        };
        if on && !is_window(overlay) {
            let made = create_overlay(editor);
            let _ = with_frame(app, |f| {
                if f.editor.hwnd == editor {
                    f.overlay = made;
                } else {
                    destroy_overlay(made);
                }
            });
        }
        relayout(app);
    })
    .await
}

// x, y: how far the editor is scrolled in its viewport (physical px, the
// page's scrollLeft / scrollTop times devicePixelRatio); kept in range.
#[tauri::command]
pub async fn plugin_frame_scroll(app: AppHandle, x: f64, y: f64) -> Result<(), String> {
    on_main(&app, move |app| {
        let changed = with_frame(app, |f| {
            let want = (x.round() as i32, y.round() as i32);
            let moved = f.view.is_some() && want != f.scroll;
            if moved {
                f.scroll = want;
            }
            moved
        });
        if changed == Some(true) {
            relayout(app);
        }
    })
    .await
}

#[tauri::command]
pub async fn plugin_frame_info(app: AppHandle) -> Option<FrameInfo> {
    with_frame(&app, |f| info(f))
}

// ---------- main window ----------

pub fn remember_main(main: &WebviewWindow) {
    if let Ok(h) = main.hwnd() {
        MAIN.store(h.0 as isize, Ordering::Relaxed);
    }
    let _ = APP.set(main.app_handle().clone());
}

// Window events (main thread): the frame follows the main window, and a frame
// closed from outside (Alt+F4) closes like its close button does.
pub fn on_window_event(window: &Window, event: &WindowEvent) {
    let app = window.app_handle();
    match (window.label(), event) {
        ("main", WindowEvent::Moved(_)) => relayout(app),
        ("main", WindowEvent::Resized(_)) => {
            with_frame(app, |f| f.fit = None);
            relayout(app);
        }
        // Resized by its edges (WM_SIZING already sized the editor): centre and
        // re-clip it for the new size.
        (LABEL, WindowEvent::Resized(_)) => {
            if with_frame(app, |f| f.mode == Mode::Normal && f.editor.resizable) == Some(true) {
                relayout(app);
            }
        }
        (LABEL, WindowEvent::CloseRequested { api, .. }) => {
            api.prevent_close();
            close(app, None);
        }
        // Destroyed without close() (the main window went away first). After
        // close() there is nothing left to do: it took the state before
        // destroying the window.
        (LABEL, WindowEvent::Destroyed) => close(app, None),
        (LABEL, WindowEvent::ScaleFactorChanged { .. }) | ("main", WindowEvent::ScaleFactorChanged { .. }) => {
            // After the window has taken its new DPI size, from a later turn
            // of the event loop. (tao also re-applies the frame's window flags
            // then, and since the frame is shown with SetWindowPos rather than
            // show(), which would activate it, tao believes it hidden: the
            // relayout shows it again.)
            let a = app.clone();
            std::thread::spawn(move || {
                let b = a.clone();
                let _ = a.run_on_main_thread(move || relayout(&b));
            });
        }
        _ => {}
    }
}
