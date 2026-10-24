use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::{process::CommandEvent, ShellExt};
use tokio::sync::{oneshot, Mutex};

type Pending = Arc<Mutex<HashMap<String, oneshot::Sender<Result<Value, String>>>>>;

struct CoreProcess {
    generation: u64,
    child: tauri_plugin_shell::process::CommandChild,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct CorePaths {
    pub data_dir: PathBuf,
    pub log_dir: PathBuf,
    pub secrets_dir: PathBuf,
    pub paperlist_csv: PathBuf,
    pub browser_dir: PathBuf,
}

#[derive(Default)]
pub struct CoreManager {
    process: Arc<Mutex<Option<CoreProcess>>>,
    pending: Pending,
    next_id: AtomicU64,
}

impl CoreManager {
    pub fn paths(app: &AppHandle) -> Result<CorePaths, String> {
        let root = app.path().app_data_dir().map_err(|e| e.to_string())?;
        let log_dir = app.path().app_log_dir().map_err(|e| e.to_string())?;
        let resource_dir = app.path().resource_dir().map_err(|e| e.to_string())?;
        let bundled_csv = resource_dir.join("archive").join("报纸清单.csv");
        let paperlist_csv = if cfg!(debug_assertions) && !bundled_csv.exists() {
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("..")
                .join("archive")
                .join("报纸清单.csv")
        } else {
            bundled_csv
        };
        Ok(CorePaths {
            data_dir: root.join("data"),
            log_dir,
            secrets_dir: root.join("secrets"),
            paperlist_csv,
            browser_dir: root.join("playwright-browsers"),
        })
    }

    pub fn ensure_paths(app: &AppHandle) -> Result<CorePaths, String> {
        let paths = Self::paths(app)?;
        for path in [&paths.data_dir, &paths.log_dir, &paths.secrets_dir] {
            std::fs::create_dir_all(path)
                .map_err(|e| format!("创建 {} 失败: {e}", path.display()))?;
        }
        Ok(paths)
    }

    async fn ensure_started(&self, app: &AppHandle) -> Result<(), String> {
        let mut slot = self.process.lock().await;
        if slot.is_some() {
            return Ok(());
        }

        let paths = Self::ensure_paths(app)?;
        let generation = self.next_id.fetch_add(1, Ordering::Relaxed);
        let command = app
            .shell()
            .sidecar("newspapercrawl-core")
            .map_err(|e| format!("找不到 Python 核心: {e}"))?
            .env("NEWSPAPER_DATA_DIR", &paths.data_dir)
            .env("NEWSPAPER_LOG_DIR", &paths.log_dir)
            .env("NEWSPAPER_SECRETS_DIR", &paths.secrets_dir)
            .env("NEWSPAPER_PAPERLIST_CSV", &paths.paperlist_csv)
            .env("PLAYWRIGHT_BROWSERS_PATH", &paths.browser_dir)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8");
        let (mut rx, child) = command
            .spawn()
            .map_err(|e| format!("启动 Python 核心失败: {e}"))?;
        *slot = Some(CoreProcess { generation, child });

        let process = Arc::clone(&self.process);
        let pending = Arc::clone(&self.pending);
        let app_handle = app.clone();
        tauri::async_runtime::spawn(async move {
            while let Some(event) = rx.recv().await {
                match event {
                    CommandEvent::Stdout(bytes) => {
                        let line = String::from_utf8_lossy(&bytes);
                        let line = line.trim();
                        if line.is_empty() {
                            continue;
                        }
                        match serde_json::from_str::<Value>(line) {
                            Ok(frame) if frame.get("id").is_some() => {
                                let id = frame["id"].as_str().unwrap_or_default().to_string();
                                let result = if frame["ok"].as_bool().unwrap_or(false) {
                                    Ok(frame.get("result").cloned().unwrap_or(Value::Null))
                                } else {
                                    Err(frame["error"]
                                        .as_str()
                                        .unwrap_or("Python 核心请求失败")
                                        .to_string())
                                };
                                if let Some(sender) = pending.lock().await.remove(&id) {
                                    let _ = sender.send(result);
                                }
                            }
                            Ok(frame) => {
                                let _ = app_handle.emit("core-event", frame);
                            }
                            Err(_) => {
                                let _ = app_handle.emit(
                                    "core-event",
                                    json!({"type": "log", "message": format!("core | {line}")}),
                                );
                            }
                        }
                    }
                    CommandEvent::Stderr(bytes) => {
                        let message = String::from_utf8_lossy(&bytes).trim().to_string();
                        if !message.is_empty() {
                            let _ = app_handle.emit(
                                "core-event",
                                json!({"type": "log", "message": format!("core | {message}")}),
                            );
                        }
                    }
                    CommandEvent::Error(error) => {
                        let _ = app_handle.emit(
                            "core-event",
                            json!({"type": "log", "message": format!("Python 核心错误: {error}")}),
                        );
                    }
                    CommandEvent::Terminated(payload) => {
                        let mut guard = process.lock().await;
                        if guard
                            .as_ref()
                            .is_some_and(|current| current.generation == generation)
                        {
                            *guard = None;
                        }
                        drop(guard);
                        let senders = std::mem::take(&mut *pending.lock().await);
                        for (_, sender) in senders {
                            let _ = sender.send(Err("Python 核心已退出".to_string()));
                        }
                        let _ = app_handle.emit(
                            "core-event",
                            json!({
                                "type": "log",
                                "message": format!("Python 核心已退出: {:?}", payload)
                            }),
                        );
                    }
                    _ => {}
                }
            }
        });
        Ok(())
    }

    pub async fn request(&self, app: &AppHandle, mut request: Value) -> Result<Value, String> {
        self.ensure_started(app).await?;
        if !request.is_object() {
            return Err("核心请求必须是 JSON 对象".to_string());
        }
        let id = self.next_id.fetch_add(1, Ordering::Relaxed).to_string();
        request["id"] = Value::String(id.clone());
        let (sender, receiver) = oneshot::channel();
        self.pending.lock().await.insert(id.clone(), sender);

        let mut slot = self.process.lock().await;
        let process = slot.as_mut().ok_or("Python 核心未运行")?;
        let mut frame = serde_json::to_vec(&request).map_err(|e| e.to_string())?;
        frame.push(b'\n');
        if let Err(error) = process.child.write(&frame) {
            drop(slot);
            self.pending.lock().await.remove(&id);
            return Err(format!("发送核心请求失败: {error}"));
        }
        drop(slot);

        match tokio::time::timeout(Duration::from_secs(30), receiver).await {
            Ok(result) => result.map_err(|_| "Python 核心响应通道已关闭".to_string())?,
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err("等待 Python 核心响应超时".to_string())
            }
        }
    }

    pub async fn prepare_update(&self, app: &AppHandle) -> Result<(), String> {
        let state = self.request(app, json!({"command": "state"})).await?;
        if let Some(operation) = state["operation"].as_str() {
            return Err(format!("{operation} 仍在运行，请停止后再更新"));
        }
        self.shutdown(app).await
    }

    pub async fn shutdown(&self, app: &AppHandle) -> Result<(), String> {
        if self.process.lock().await.is_none() {
            return Ok(());
        }
        let _ = self.request(app, json!({"command": "shutdown"})).await?;
        let mut slot = self.process.lock().await;
        if let Some(process) = slot.take() {
            drop(process.child);
        }
        Ok(())
    }

    pub fn kill_now(&self) {
        if let Ok(mut slot) = self.process.try_lock() {
            if let Some(process) = slot.take() {
                let _ = process.child.kill();
            }
        }
    }
}
