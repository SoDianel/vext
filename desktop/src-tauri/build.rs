fn main() {
    // tauri-build embeds icons/icon.ico (the exe's icon, also the window icon on
    // Windows) but doesn't watch it: without this a new logo needs a cargo clean.
    println!("cargo:rerun-if-changed=icons");
    // VEXT's own commands get permissions (allow-<command>), so each webview
    // can only call what its capability grants: the main page and the plugin
    // frame get their own sets (capabilities/), the Playground's browser
    // (remote sites) gets none, and the image browser (imgsearch) only
    // imgsearch_pick. Without this list every webview of a local
    // origin could call every command.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "relaunch_elevated",
            "browser_place",
            "browser_go",
            "browser_state",
            "imgsearch_place",
            "imgsearch_go",
            "imgsearch_close",
            "imgsearch_pick",
            "plugin_frame_open",
            "plugin_frame_close",
            "plugin_frame_move",
            "plugin_frame_toggle",
            "plugin_frame_info",
            "plugin_frame_bypass",
            "plugin_frame_scroll",
        ]),
    ))
    .expect("failed to run the Tauri build script");
}
