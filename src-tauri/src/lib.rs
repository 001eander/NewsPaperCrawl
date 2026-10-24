mod core;

use core::{CoreManager, CorePaths};
use serde_json::Value;
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_opener::OpenerExt;

#[tauri::command]
async fn core_request(
    app: AppHandle,
    manager: tauri::State<'_, CoreManager>,
    request: Value,
) -> Result<Value, String> {
    manager.request(&app, request).await
}

#[tauri::command]
fn get_paths(app: AppHandle) -> Result<CorePaths, String> {
    CoreManager::ensure_paths(&app)
}

#[tauri::command]
fn open_path(app: AppHandle, kind: String) -> Result<(), String> {
    let paths = CoreManager::ensure_paths(&app)?;
    let path = match kind.as_str() {
        "data" => paths.data_dir,
        "logs" => paths.log_dir,
        "secrets" => paths.secrets_dir,
        _ => return Err("未知目录类型".to_string()),
    };
    app.opener()
        .open_path(path.to_string_lossy(), None::<String>)
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn prepare_update(
    app: AppHandle,
    manager: tauri::State<'_, CoreManager>,
) -> Result<(), String> {
    manager.prepare_update(&app).await
}

#[tauri::command]
async fn shutdown_core(
    app: AppHandle,
    manager: tauri::State<'_, CoreManager>,
) -> Result<(), String> {
    manager.shutdown(&app).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(CoreManager::default())
        .invoke_handler(tauri::generate_handler![
            core_request,
            get_paths,
            open_path,
            prepare_update,
            shutdown_core
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                app.state::<CoreManager>().kill_now();
            }
        });
}
