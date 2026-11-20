pub mod engine;
pub mod win32;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU64, Ordering};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, PhysicalPosition, RunEvent, State, Window, WindowEvent};

/// 由 Tauri 托管的引擎运行时状态。
pub struct EngineState {
    /// 引擎实际监听端口：壳先分配一个，读到引擎自报端口后覆盖。
    pub port: AtomicU16,
    /// 32 位 hex，只通过环境变量 `VT_TOKEN` 交给引擎子进程。
    pub token: String,
    /// 解析好的启动配置：重启时直接复用。
    pub config: Mutex<Option<engine::EngineConfig>>,
    pub status: Mutex<engine::EngineStatus>,
    pub child: Mutex<Option<std::process::Child>>,
    /// 作业对象：壳退出/崩溃时连引擎派生出来的 ffmpeg 孙进程一起收走。
    pub job: Mutex<Option<win32::JobObject>>,
    pub stopping: AtomicBool,
    /// 每一轮启动的代号：restart / shutdown 递增，旧监督线程据此放弃写状态。
    pub epoch: AtomicU64,
    /// 串行化 `engine_restart`。
    pub restart_lock: Mutex<()>,
    pub log_path: PathBuf,
    /// `<data>/port.json`：壳关闭时也删一次（引擎 try/finally 可能来不及）。
    pub port_file: PathBuf,
}

#[derive(Serialize)]
pub struct EngineInfo {
    pub port: u16,
    pub token: String,
}

/// `engine_restart` 的返回体（契约：`{ok:boolean}`）。
#[derive(Serialize)]
pub struct EngineRestart {
    pub ok: bool,
}

#[derive(Serialize, Deserialize)]
struct SavedWindowPosition {
    x: i32,
    y: i32,
}

fn window_state_path() -> PathBuf {
    engine::default_data_dir().join("window-state.json")
}

fn load_window_position() -> Option<PhysicalPosition<i32>> {
    let raw = std::fs::read_to_string(window_state_path()).ok()?;
    let saved: SavedWindowPosition = serde_json::from_str(&raw).ok()?;
    Some(PhysicalPosition::new(saved.x, saved.y))
}

/// 只恢复“看得见”的位置：至少要有 200×120 落在某块屏幕内，
/// 避免拔掉副屏后窗口跑到不存在的坐标上。
fn position_is_visible(window: &tauri::WebviewWindow, pos: PhysicalPosition<i32>) -> bool {
    let Ok(monitors) = window.available_monitors() else {
        return false;
    };
    monitors.iter().any(|monitor| {
        let origin = monitor.position();
        let size = monitor.size();
        let left = origin.x;
        let top = origin.y;
        let right = left + size.width as i32;
        let bottom = top + size.height as i32;
        pos.x + 200 >= left
            && pos.x <= right - 200
            && pos.y + 120 >= top
            && pos.y <= bottom - 120
    })
}

fn save_window_position(window: &Window) {
    if window.is_maximized().unwrap_or(false) {
        return;
    }
    let Ok(pos) = window.outer_position() else {
        return;
    };
    let saved = SavedWindowPosition { x: pos.x, y: pos.y };
    if let Ok(raw) = serde_json::to_string(&saved) {
        if let Some(parent) = window_state_path().parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::write(window_state_path(), raw);
    }
}

#[tauri::command]
fn engine_info(state: State<'_, EngineState>) -> EngineInfo {
    EngineInfo {
        port: state.port.load(Ordering::SeqCst),
        token: state.token.clone(),
    }
}

/// 契约：@{phase, pid?, exit_code?, log_tail?(≤4 KiB), error?}@。
#[tauri::command]
fn engine_status(state: State<'_, EngineState>) -> engine::EngineStatusReport {
    engine::status_report(state.inner())
}

/// 契约：幂等重启，返回 `{ok:boolean}`；失败原因写在 `engine_status` 里。
#[tauri::command]
fn engine_restart(app: AppHandle) -> EngineRestart {
    EngineRestart {
        ok: engine::restart(&app),
    }
}

/// 在资源管理器中定位文件/目录（规格第 5 节的 open_path）。
#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    let target = PathBuf::from(&path);
    if !target.exists() {
        return Err(format!("路径不存在：{path}"));
    }

    #[cfg(windows)]
    {
        let mut cmd = std::process::Command::new("explorer.exe");
        if target.is_dir() {
            cmd.arg(&target);
        } else {
            cmd.arg(format!("/select,{}", target.display()));
        }
        cmd.spawn()
            .map(|_| ())
            .map_err(|e| format!("无法打开资源管理器：{e}"))
    }
    #[cfg(not(windows))]
    {
        let _ = target;
        Err("open_path 仅支持 Windows".to_string())
    }
}

pub fn run() {
    // 便携数据目录：优先 exe 同级 data/，不可写才回落 %LOCALAPPDATA%\VoiceTrace。
    // 先建出来，单实例的互斥量名字才能基于规范化路径稳定下来。
    let data_dir = engine::default_data_dir();
    let _ = std::fs::create_dir_all(&data_dir);

    // 单实例：互斥量名字带数据目录哈希，不同数据目录可以各开一个实例。
    if win32::already_running(&data_dir) {
        win32::focus_existing();
        win32::message(
            "声迹已经在运行。\n\n已把正在运行的窗口切到前台。",
            win32::WINDOW_TITLE,
        );
        return;
    }

    // WebView2 用户数据也放进同一目录，避免受限环境写 AppData 失败导致建窗崩溃。
    let webview_dir = data_dir.join("webview");
    let _ = std::fs::create_dir_all(&webview_dir);
    std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &webview_dir);
    let log_path = data_dir.join("logs").join("engine.log");
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    // panic 不再静默 abort：写 panic.log 并弹窗，用户至少知道发生了什么。
    {
        let panic_log = data_dir.join("logs").join("panic.log");
        let fallback = log_path.clone();
        std::panic::set_hook(Box::new(move |info| {
            let text = format!("声迹遇到未处理的错误：\n{info}");
            let target = if panic_log.parent().map(|p| p.exists()).unwrap_or(false) {
                &panic_log
            } else {
                &fallback
            };
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(target)
            {
                use std::io::Write;
                let _ = writeln!(f, "\n{text}");
            }
            eprintln!("{text}");
            win32::message(&text, win32::WINDOW_TITLE);
        }));
    }

    let setup_log_path = log_path.clone();
    let setup_data_dir = data_dir.clone();
    let app = match tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            engine_info,
            engine_status,
            engine_restart,
            open_path
        ])
        .setup(move |app| {
            // 端口由引擎自 bind(0) 后上报（契约）；壳不再预选，避免“探测到
            // 空闲→释放→被别的进程抢占”的竞态，重启也不会复用冲突端口。
            let port = 0u16;
            let token_result = engine::generate_token();
            let token = token_result.as_ref().map(|token| token.clone()).unwrap_or_default();
            let config_result = engine::EngineConfig::resolve();
            let config = config_result.as_ref().map(|config| config.clone()).ok();
            let state = EngineState {
                port: AtomicU16::new(port),
                token: token.clone(),
                config: Mutex::new(config.clone()),
                status: Mutex::new(engine::EngineStatus::new("starting", "正在启动转录引擎…")),
                child: Mutex::new(None),
                job: Mutex::new(None),
                stopping: AtomicBool::new(false),
                epoch: AtomicU64::new(0),
                restart_lock: Mutex::new(()),
                log_path: setup_log_path.clone(),
                port_file: setup_data_dir.join(engine::PORT_FILE_NAME),
            };
            app.manage(state);

            // 尺寸每次回归 1600×900，只恢复上次关闭时的位置。
            if let Some(window) = app.get_webview_window("main") {
                match load_window_position() {
                    Some(pos) if position_is_visible(&window, pos) => {
                        let _ = window.set_position(pos);
                    }
                    _ => {
                        let _ = window.center();
                    }
                }
                let _ = window.show();
                let _ = window.set_focus();
            }

            match (token_result, config, config_result) {
                (Ok(_), Some(config), _) => {
                    let handle = app.handle().clone();
                    std::thread::spawn(move || engine::supervise(handle, config, port, token, 0));
                }
                (Err(e), _, _) => {
                    // 没有 CSPRNG 就没有 token，引擎起来也连不上：报错但不挡窗口。
                    engine::set_phase(app.handle(), "failed", &format!("无法生成访问令牌：{e}"), None, None);
                }
                (_, None, Err(e)) => {
                    // 路径问题也要让窗口正常出现，由启动页读取 engine_status 展示错误。
                    let msg = format!("无法定位转录引擎：{e}\n\n应用仍可打开，请检查安装是否完整。");
                    engine::set_phase(app.handle(), "failed", &msg, None, None);
                }
                _ => {}
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { .. } = event {
                save_window_position(window);
                engine::shutdown(window.app_handle());
            }
        })
        .build(tauri::generate_context!())
    {
        Ok(app) => app,
        Err(e) => {
            let msg = format!(
                "界面创建失败：{e}\n\n这通常与 WebView2 运行环境有关。\n日志：{}",
                log_path.display()
            );
            eprintln!("{msg}");
            win32::message(&msg, win32::WINDOW_TITLE);
            std::process::exit(1);
        }
    };

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            engine::shutdown(app_handle);
        }
    });
}
