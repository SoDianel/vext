use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::time::Duration;

// Resolve again in the backend at drag time: the page cannot supply an arbitrary file path.
fn resolve(plugin_id: Option<i64>, key: &str) -> Result<PathBuf, String> {
    let config = crate::config();
    let body = serde_json::json!({"plugin_id": plugin_id, "key": key}).to_string();
    let mut socket = TcpStream::connect(("127.0.0.1", config.port)).map_err(|e| e.to_string())?;
    socket
        .set_read_timeout(Some(Duration::from_secs(15)))
        .map_err(|e| e.to_string())?;
    socket
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;
    let request = format!(
        "POST /api/fl-launch/drag-resolve HTTP/1.0\r\nHost: 127.0.0.1\r\nX-VEXT-Token: {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        config.token, body.len(), body
    );
    socket
        .write_all(request.as_bytes())
        .map_err(|e| e.to_string())?;
    let mut response = String::new();
    socket
        .take(65536)
        .read_to_string(&mut response)
        .map_err(|e| e.to_string())?;
    let (head, body) = response
        .split_once("\r\n\r\n")
        .ok_or("Invalid backend response")?;
    let value: serde_json::Value = serde_json::from_str(body).map_err(|e| e.to_string())?;
    if !head.lines().next().unwrap_or("").contains(" 200 ") {
        return Err(value["detail"]
            .as_str()
            .unwrap_or("Preset unavailable")
            .to_owned());
    }
    let path = PathBuf::from(value["path"].as_str().ok_or("Missing preset path")?);
    let path = path.canonicalize().map_err(|e| e.to_string())?;
    if !path.is_file()
        || !path
            .extension()
            .is_some_and(|e| e.eq_ignore_ascii_case("fst"))
    {
        return Err("The FL preset is no longer available".into());
    }
    Ok(path)
}

#[tauri::command]
pub async fn fl_drag_preset(
    window: tauri::WebviewWindow,
    plugin_id: Option<i64>,
    key: String,
) -> Result<bool, String> {
    if !matches!(window.label(), "main" | "fl-bubble") {
        return Err("Only the main VEXT page or the FL bubble may drag presets".into());
    }
    let path = tauri::async_runtime::spawn_blocking(move || resolve(plugin_id, &key))
        .await
        .map_err(|e| e.to_string())??;
    let (sender, receiver) = std::sync::mpsc::channel();
    let drag_window = window.clone();
    window
        .run_on_main_thread(move || {
            use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON};
            // Resolution crosses IPC twice. A release during either lookup must cancel the drag.
            if unsafe { GetAsyncKeyState(VK_LBUTTON.0 as i32) } >= 0 {
                let _ = sender.send(Ok(false));
                return;
            }
            let callback = sender.clone();
            let result = drag::start_drag(
                &drag_window,
                drag::DragItem::Files(vec![path]),
                drag::Image::Raw(include_bytes!("../icons/32x32.png").to_vec()),
                move |result, _| {
                    let _ = callback.send(Ok(matches!(result, drag::DragResult::Dropped)));
                },
                drag::Options::default(),
            );
            if let Err(error) = result {
                let _ = sender.send(Err(error.to_string()));
            }
        })
        .map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || receiver.recv().map_err(|e| e.to_string())?)
        .await
        .map_err(|e| e.to_string())?
}
