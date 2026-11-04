use std::path::Path;
use std::time::{Duration, Instant};

use windows::core::PWSTR;
use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM};
use windows::Win32::System::Threading::{
    OpenProcess, QueryFullProcessImageNameW, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetForegroundWindow, GetWindow, GetWindowTextW, GetWindowThreadProcessId,
    IsIconic, IsWindowVisible, SetForegroundWindow, ShowWindow, GW_OWNER, SW_RESTORE,
};

unsafe extern "system" fn find_fl(hwnd: HWND, context: LPARAM) -> windows::core::BOOL {
    if !unsafe { IsWindowVisible(hwnd) }.as_bool() {
        return true.into();
    }
    if unsafe { GetWindow(hwnd, GW_OWNER) }.is_ok_and(|owner| !owner.0.is_null()) {
        return true.into();
    }
    let mut title = [0u16; 512];
    let length = unsafe { GetWindowTextW(hwnd, &mut title) };
    if length <= 0 || !String::from_utf16_lossy(&title[..length as usize]).contains("FL Studio") {
        return true.into();
    }
    let mut pid = 0;
    unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid)) };
    let Ok(process) = (unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) })
    else {
        return true.into();
    };
    let mut image = vec![0u16; 32768];
    let mut size = image.len() as u32;
    let queried = unsafe {
        QueryFullProcessImageNameW(
            process,
            PROCESS_NAME_WIN32,
            PWSTR(image.as_mut_ptr()),
            &mut size,
        )
    };
    let _ = unsafe { CloseHandle(process) };
    if queried.is_ok() {
        let image = String::from_utf16_lossy(&image[..size as usize]);
        let name = Path::new(&image)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_ascii_lowercase();
        if matches!(
            name.as_str(),
            "fl.exe" | "fl64.exe" | "fl (scaled).exe" | "fl64 (scaled).exe"
        ) {
            // EnumWindows invokes this callback synchronously while the vector remains alive.
            unsafe { &mut *(context.0 as *mut Vec<HWND>) }.push(hwnd);
        }
    }
    true.into()
}

#[tauri::command]
pub async fn fl_focus(window: tauri::WebviewWindow) -> Result<(), String> {
    if !matches!(window.label(), "main" | "fl-bubble") {
        return Err("Only the main VEXT page or the FL bubble may hand focus to FL Studio".into());
    }
    tauri::async_runtime::spawn_blocking(focus_fl_window)
        .await
        .map_err(|error| error.to_string())?
}

fn focus_fl_window() -> Result<(), String> {
    let mut candidates = Vec::<HWND>::new();
    unsafe { EnumWindows(Some(find_fl), LPARAM(&mut candidates as *mut _ as isize)) }
        .map_err(|e| e.to_string())?;
    if candidates.len() != 1 {
        return Err(if candidates.is_empty() {
            "Open FL Studio before loading a plugin."
        } else {
            "More than one FL Studio window is open. Keep only the target instance open."
        }
        .into());
    }
    let target = candidates[0];
    let mut foreground_pid = 0;
    let mut target_pid = 0;
    unsafe {
        GetWindowThreadProcessId(GetForegroundWindow(), Some(&mut foreground_pid));
        GetWindowThreadProcessId(target, Some(&mut target_pid));
    }
    if foreground_pid != std::process::id() && foreground_pid != target_pid {
        return Err("Select VEXT and click Load again so it can hand focus to FL Studio.".into());
    }
    if unsafe { IsIconic(target) }.as_bool() {
        let _ = unsafe { ShowWindow(target, SW_RESTORE) };
    }
    let _ = unsafe { SetForegroundWindow(target) };
    // Activation crosses UI threads. Wait off VEXT's UI thread for its message to complete.
    let deadline = Instant::now() + Duration::from_millis(250);
    while unsafe { GetForegroundWindow() } != target {
        if Instant::now() >= deadline {
            return Err(
                "Windows did not give FL Studio focus. Bring FL forward, then try again.".into(),
            );
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    Ok(())
}
