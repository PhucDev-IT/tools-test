mod core;

use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;
use tauri::{Manager, RunEvent, State};

use crate::core::{CoreManager, CoreState};

type Core<'a> = State<'a, Arc<CoreManager>>;

/// Single entry point from React to the Agent Core. The Core validates method + params.
#[tauri::command]
async fn core_request(core: Core<'_>, method: String, params: Option<Value>, timeout_ms: Option<u64>) -> Result<Value, Value> {
    let timeout = Duration::from_millis(timeout_ms.unwrap_or(30_000).clamp(1_000, 120_000));
    core.request(&method, params.unwrap_or_else(|| Value::Object(Default::default())), timeout).await
}

#[tauri::command]
fn core_state(core: Core<'_>) -> CoreState {
    core.state()
}

#[tauri::command]
fn core_stderr(core: Core<'_>) -> Vec<String> {
    core.stderr_tail()
}

#[tauri::command]
async fn core_start(app: tauri::AppHandle, core: Core<'_>) -> Result<CoreState, String> {
    core.inner().start(&app).await
}

#[tauri::command]
async fn core_stop(app: tauri::AppHandle, core: Core<'_>) -> Result<CoreState, String> {
    Ok(core.stop(&app).await)
}

#[tauri::command]
async fn core_restart(app: tauri::AppHandle, core: Core<'_>) -> Result<CoreState, String> {
    core.stop(&app).await;
    core.inner().start(&app).await
}

pub fn run() {
    let manager = Arc::new(CoreManager::new());
    let app = tauri::Builder::default()
        // Must be first: a second launch focuses the existing window instead of starting
        // another GUI (which would try to start a second Core).
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(manager)
        .invoke_handler(tauri::generate_handler![core_request, core_state, core_stderr, core_start, core_stop, core_restart])
        .setup(|app| {
            let handle = app.handle().clone();
            let core = app.state::<Arc<CoreManager>>().inner().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(message) = core.start(&handle).await {
                    eprintln!("Agent Core start failed: {message}");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("không khởi tạo được ứng dụng Sarae Agent");

    app.run(|handle, event| {
        if let RunEvent::ExitRequested { api, .. } = &event {
            let core = handle.state::<Arc<CoreManager>>().inner().clone();
            if core.is_running() && core.begin_exit() {
                // Keep the process alive until the Core has shut down cleanly.
                api.prevent_exit();
                let handle = handle.clone();
                tauri::async_runtime::spawn(async move {
                    core.stop(&handle).await;
                    handle.exit(0);
                });
            }
        }
    });
}
