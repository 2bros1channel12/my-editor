mod service;

use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use tauri::State;

#[derive(Default)]
struct AppState {
    service: Mutex<service::ServiceClient>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ServiceCall {
    method: String,
    params: serde_json::Value,
}

#[tauri::command]
fn service_request(app: tauri::AppHandle, state: State<'_, AppState>, call: ServiceCall) -> Result<serde_json::Value, String> {
    if !service::supports_method(&call.method) {
        return Err("Unsupported service method".to_string());
    }
    state.service.lock().map_err(|_| "Service supervisor lock is unavailable".to_string())?.request(&app, &call.method, call.params)
}

#[tauri::command]
fn shell_health() -> serde_json::Value {
    serde_json::json!({ "available": true, "version": env!("CARGO_PKG_VERSION") })
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![service_request, shell_health])
        .run(tauri::generate_context!())
        .expect("failed to run Local Cut desktop shell");
}
