use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use crate::win32;
use crate::EngineState;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// Windows `CREATE_NO_WINDOW`：Python 子进程不弹控制台。
pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 健康检查最长等待 180 秒（模型首次加载可能较慢）。
pub const HEALTH_TIMEOUT: Duration = Duration::from_secs(180);

/// 端口发现最长等 180 秒：壳传 --port 0，真实端口由引擎导入依赖并自 bind 后经
/// stdout/port.json 上报。旧值 5 秒在冷启动（杀软扫描、首次导入依赖）时会先超时，
/// 把 fallback=0 当端口去健康检查，结果是「引擎其实起来了、壳却永远连不上」，
/// 只能靠重启（文件缓存已热）碰巧恢复。引擎早退仍由 cancelled() 秒级失败，不等满。
pub const PORT_DISCOVERY_TIMEOUT: Duration = Duration::from_secs(180);

/// `engine_status.log_tail` 的上限：4 KiB。
pub const LOG_TAIL_LIMIT: usize = 4 * 1024;

/// `port.json` 文件名（数据目录下）。
pub const PORT_FILE_NAME: &str = "port.json";

/// 引擎状态（内部）。`phase` 只取 starting / ready / failed / exited，
/// 对外的 `engine_status` 由 [`EngineStatusReport`] 补齐 log_tail / error。
#[derive(Clone, Debug)]
pub struct EngineStatus {
    pub phase: String,
    pub message: String,
    pub pid: Option<u32>,
    pub exit_code: Option<i32>,
}

impl EngineStatus {
    pub fn new(phase: &str, message: &str) -> Self {
        Self {
            phase: phase.to_string(),
            message: message.to_string(),
            pid: None,
            exit_code: None,
        }
    }
}

/// `engine_status` 的返回体：@{phase, pid?, exit_code?, log_tail?, error?}@。
#[derive(Clone, Serialize)]
pub struct EngineStatusReport {
    pub phase: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    /// `<data>/logs/engine.log` 末尾至多 [`LOG_TAIL_LIMIT`] 字节。
    pub log_tail: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// 运行模式与所有外部路径。开发模式与便携模式共用同一套启动参数。
#[derive(Clone, Debug)]
pub struct EngineConfig {
    pub mode: &'static str,
    pub python: PathBuf,
    pub cwd: PathBuf,
    pub models_dir: PathBuf,
    pub ffmpeg_dir: PathBuf,
    pub data_dir: PathBuf,
    pub log_path: PathBuf,
    /// 写入子进程 PYTHONPATH 的唯一目录。
    pub python_path: PathBuf,
}

pub fn default_data_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("VOICETRACE_DATA_DIR") {
        return PathBuf::from(dir);
    }
    // 便携优先：exe 同级 data/ 可写就用它（数据随身走，也不受“工作区镜像禁止写 AppData”限制）。
    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            let candidate = exe_dir.join("data");
            if ensure_writable(&candidate) {
                return candidate;
            }
        }
    }
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("USERPROFILE").map(|p| PathBuf::from(p).join("AppData").join("Local")))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("VoiceTrace")
}

/// 建目录并实际写一个探针文件，确认不是“看似存在但不可写”。
fn ensure_writable(dir: &Path) -> bool {
    if std::fs::create_dir_all(dir).is_err() {
        return false;
    }
    let probe = dir.join(".write-test");
    match std::fs::write(&probe, b"ok") {
        Ok(()) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

fn log_path_for(data_dir: &Path) -> PathBuf {
    data_dir.join("logs").join("engine.log")
}

/// `<data>/port.json`：新契约只含 @{port, version, pid}@，<b>不含 token</b>。
/// 解析对多余字段保持宽容（旧版 hello 里的 token / type 会被忽略），
/// 所以“没有 token 的 port.json”不会报错。
#[derive(Clone, Debug, PartialEq, Eq, Deserialize)]
pub struct PortFile {
    pub port: u16,
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub pid: u32,
}

pub fn port_file_path(data_dir: &Path) -> PathBuf {
    data_dir.join(PORT_FILE_NAME)
}

/// 只认 `port`：缺 token、缺 version、缺 pid 都不算错。
pub fn parse_port_file(text: &str) -> Result<PortFile, String> {
    let parsed: PortFile =
        serde_json::from_str(text).map_err(|e| format!("port.json 解析失败：{e}"))?;
    if parsed.port == 0 {
        return Err("port.json 的 port 为 0".to_string());
    }
    Ok(parsed)
}

pub fn read_port_file(path: &Path) -> Result<PortFile, String> {
    let text =
        std::fs::read_to_string(path).map_err(|e| format!("读取 port.json 失败：{e}"))?;
    parse_port_file(&text)
}

impl EngineConfig {
    /// 生产（便携）优先：exe 同级有 `runtime/python/python.exe` 就按便携布局解析；
    /// 否则按开发布局，从 exe 位置与 `CARGO_MANIFEST_DIR` 向上找含 `#source/engine` 的仓库根。
    pub fn resolve() -> Result<Self, String> {
        let exe = std::env::current_exe().map_err(|e| format!("无法获取 exe 路径：{e}"))?;
        let exe_dir = exe
            .parent()
            .ok_or_else(|| "无法获取 exe 所在目录".to_string())?
            .to_path_buf();
        let data_dir = default_data_dir();
        let log_path = log_path_for(&data_dir);

        let portable_python = exe_dir.join("runtime").join("python").join("python.exe");
        if portable_python.exists() {
            let engine_dir = exe_dir.join("engine");
            return Ok(Self {
                mode: "portable",
                python: portable_python,
                cwd: engine_dir.clone(),
                models_dir: exe_dir.join("models"),
                ffmpeg_dir: exe_dir.join("runtime"),
                data_dir,
                log_path,
                python_path: exe_dir.join("runtime").join("site-packages"),
            });
        }

        let root = find_repo_root(&exe_dir)
            .ok_or_else(|| "开发模式找不到仓库根（向上未发现 #source/engine）".to_string())?;
        let engine_dir = std::env::var_os("VOICETRACE_ENGINE_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("#source").join("engine"));
        let python = std::env::var_os("VOICETRACE_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| engine_dir.join(".venv").join("Scripts").join("python.exe"));
        if !python.exists() {
            return Err(format!(
                "开发模式找不到 Python：{}（可设 VOICETRACE_PYTHON 覆盖）",
                python.display()
            ));
        }
        let ffmpeg_dir = std::env::var_os("VOICETRACE_FFMPEG_DIR")
            .map(PathBuf::from)
            .or_else(find_ffmpeg_in_path)
            .unwrap_or_else(|| PathBuf::from("ffmpeg"));

        Ok(Self {
            mode: "dev",
            python,
            cwd: engine_dir.clone(),
            models_dir: engine_dir.join("models"),
            ffmpeg_dir,
            data_dir,
            log_path,
            // 开发模式让 `-m voicetrace_engine` 能从 cwd/PYTHONPATH 找到包。
            python_path: engine_dir,
        })
    }
}

fn find_repo_root(start: &Path) -> Option<PathBuf> {
    let mut starts = vec![start.to_path_buf()];
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    if manifest != start {
        starts.push(manifest);
    }
    for base in starts {
        for ancestor in base.ancestors() {
            if ancestor
                .join("#source")
                .join("engine")
                .join(".venv")
                .join("Scripts")
                .join("python.exe")
                .exists()
            {
                return Some(ancestor.to_path_buf());
            }
        }
    }
    None
}

fn find_ffmpeg_in_path() -> Option<PathBuf> {
    // 常见位置优先，避免依赖 PATH 的当前状态。
    for candidate in [r"D:\ffmpeg\bin", r"C:\ffmpeg\bin"] {
        let dir = PathBuf::from(candidate);
        if dir.join("ffmpeg.exe").exists() {
            return Some(dir);
        }
    }
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        if dir.join("ffmpeg.exe").exists() {
            return Some(dir);
        }
    }
    None
}

/// 绑定 127.0.0.1:0 拿到一个空闲端口后立刻释放。
/// 产品路径已改为「引擎自 bind(0) 上报」（见 D4）；保留此函数供 supervisor 测试使用。
#[allow(dead_code)]
pub fn pick_free_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|e| format!("无法分配本地端口：{e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("无法读取本地端口：{e}"))?
        .port();
    drop(listener);
    Ok(port)
}

/// 32 位小写十六进制 token：16 字节 CSPRNG（Win32 `BCryptGenRandom`；其他平台 `/dev/urandom`）。
/// 取不到随机数就是错误，绝不退化成 uuid / 时间种子。
pub fn generate_token() -> Result<String, String> {
    let mut bytes = [0u8; 16];
    win32::random_bytes(&mut bytes)?;
    let mut token = String::with_capacity(32);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(token, "{byte:02x}");
    }
    Ok(token)
}

/// 启动成功的引擎进程：作业对象与 stdout 端口发现槽一起交回监督线程。
pub struct SpawnedEngine {
    pub child: Child,
    pub job: Option<win32::JobObject>,
    pub stdout_port: Arc<Mutex<Option<u16>>>,
}

/// 启动 Python 引擎，stderr（含引擎自身日志）追加写入 `<data-dir>/logs/engine.log`。
/// token 只走环境变量 `VT_TOKEN`：命令行参数会出现在进程列表里，任何本机进程都能读。
/// Windows 下带 `CREATE_NO_WINDOW`，不会闪控制台。
pub fn spawn_engine(config: &EngineConfig, port: u16, token: &str) -> Result<SpawnedEngine, String> {
    if let Some(parent) = config.log_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("无法创建日志目录 {}：{e}", parent.display()))?;
    }
    std::fs::create_dir_all(&config.data_dir)
        .map_err(|e| format!("无法创建数据目录 {}：{e}", config.data_dir.display()))?;

    // 子进程的 stdout/stderr 写 engine.out.log，engine.log 留给 Python 侧的
    // RotatingFileHandler —— 两边抢同一个文件时，Windows 上的 rename 会被句柄挡住。
    let child_log_path = config.log_path.with_file_name("engine.out.log");

    // 引擎日志超过 5 MB 就轮转一次，避免无限增长（旧版递归自追加曾把日志撑到数 MB）。
    if let Ok(meta) = std::fs::metadata(&child_log_path) {
        if meta.len() > 5 * 1024 * 1024 {
            let prev = child_log_path.with_file_name("engine.out.prev.log");
            let _ = std::fs::remove_file(&prev);
            let _ = std::fs::rename(&child_log_path, &prev);
        }
    }

    let mut log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&child_log_path)
        .map_err(|e| format!("无法打开引擎日志 {}：{e}", child_log_path.display()))?;
    let _ = writeln!(
        log,
        "\n=== [{}] start engine mode={} port={} python={} ===",
        unix_seconds(),
        config.mode,
        port,
        config.python.display()
    );
    let _ = log.flush();

    let mut cmd = Command::new(&config.python);
    cmd.arg("-m")
        .arg("voicetrace_engine")
        // 引擎自身固定绑定 127.0.0.1；其 argparse 不接受 --host，传了会直接退出。
        .arg("--port")
        .arg(port.to_string())
        .arg("--data-dir")
        .arg(&config.data_dir)
        .arg("--models-dir")
        .arg(&config.models_dir)
        .arg("--ffmpeg")
        .arg(&config.ffmpeg_dir)
        // 父进程退出（含崩溃）后引擎自行退出，避免残留 python 占用显存。
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .current_dir(&config.cwd)
        // token 只从环境变量传：不足 32 位 hex 由壳自己保证；空 token 引擎会直接退出（契约）。
        .env("VT_TOKEN", token)
        .env("PYTHONPATH", &config.python_path)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1")
        // 便携目录是交付物：不要让它生成 __pycache__（也会在只读介质上失败）。
        .env("PYTHONDONTWRITEBYTECODE", "1")
        // 忽略用户级 site-packages，避免外部包装污染便携运行时。
        .env("PYTHONNOUSERSITE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(log));

    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("启动引擎失败（{}）：{e}", config.python.display()))?;

    // stdout 里有引擎的 hello（含真实端口）。这条流同时做两件事：落日志、发现端口。
    let stdout_port = pump_stdout(child.stdout.take(), &child_log_path, token);

    // 作业对象必须在引擎派生 ffmpeg 之前挂上；失败不阻断启动，留一行日志并让 kill_tree 兜底。
    let pid = child.id();
    let job = win32::JobObject::create();
    match &job {
        Some(job) => {
            if !job.assign_pid(pid) {
                append_note(
                    &config.log_path,
                    "job: AssignProcessToJobObject 失败，退出时改用 taskkill 兜底",
                );
            }
        }
        None => append_note(
            &config.log_path,
            "job: CreateJobObject/SetInformationJobObject 失败，退出时改用 taskkill 兜底",
        ),
    }

    Ok(SpawnedEngine {
        child,
        job,
        stdout_port,
    })
}

fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

/// 把子进程 stdout 逐行写进引擎日志，并顺手从 JSON 行里读出 `port`。
/// hello 里若回显 token，落盘前替换成 `<redacted>`：日志不能成为第二个 token 泄漏点。
fn pump_stdout(
    stdout: Option<ChildStdout>,
    log_path: &Path,
    secret: &str,
) -> Arc<Mutex<Option<u16>>> {
    let discovered: Arc<Mutex<Option<u16>>> = Arc::new(Mutex::new(None));
    let Some(stdout) = stdout else {
        return discovered;
    };
    let slot = Arc::clone(&discovered);
    let log_path = log_path.to_path_buf();
    let secret = secret.to_string();
    std::thread::spawn(move || {
        let mut sink = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .ok();
        let mut reader = BufReader::new(stdout);
        let mut raw = Vec::new();
        loop {
            raw.clear();
            match reader.read_until(b'\n', &mut raw) {
                Ok(0) => break,
                Ok(_) => {}
                Err(_) => break,
            }
            let text = String::from_utf8_lossy(&raw);
            let trimmed = text.trim();
            if !trimmed.is_empty() {
                if let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed) {
                    if let Some(port) = value.get("port").and_then(|value| value.as_u64()) {
                        if let Ok(port) = u16::try_from(port) {
                            if port != 0 {
                                if let Ok(mut slot) = slot.lock() {
                                    if slot.is_none() {
                                        *slot = Some(port);
                                    }
                                }
                            }
                        }
                    }
                }
            }
            let safe = if secret.is_empty() {
                text.to_string()
            } else {
                text.replace(secret.as_str(), "<redacted>")
            };
            if let Some(sink) = sink.as_mut() {
                let _ = sink.write_all(safe.as_bytes());
                let _ = sink.flush();
            }
        }
    });
    discovered
}

fn append_note(log_path: &Path, line: &str) {
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(file, "[supervisor] {line}");
    }
}

/// 端口来源优先级：stdout hello > port.json。
/// 超时后只有调用方给了非 0 fallback 才返回它；否则明确失败，绝不把 0 当端口
/// 交给健康检查（那会白等满 180 秒，且错误信息毫无指向性）。
fn discover_port(
    stdout_port: &Arc<Mutex<Option<u16>>>,
    port_file: &Path,
    expected_pid: u32,
    fallback: u16,
    cancelled: impl FnMut() -> Option<String>,
) -> Result<u16, String> {
    discover_port_with_timeout(
        stdout_port,
        port_file,
        expected_pid,
        fallback,
        PORT_DISCOVERY_TIMEOUT,
        cancelled,
    )
}

/// 可注入超时的版本：生产用 [`PORT_DISCOVERY_TIMEOUT`]，单元测试用毫秒级超时。
fn discover_port_with_timeout(
    stdout_port: &Arc<Mutex<Option<u16>>>,
    port_file: &Path,
    expected_pid: u32,
    fallback: u16,
    timeout: Duration,
    mut cancelled: impl FnMut() -> Option<String>,
) -> Result<u16, String> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(port) = stdout_port.lock().ok().and_then(|slot| *slot) {
            return Ok(port);
        }
        if let Ok(found) = read_port_file(port_file) {
            // pid 对得上（或引擎没写 pid）才认：否则可能是上次崩溃残留的旧端口。
            if found.pid == 0 || found.pid == expected_pid {
                return Ok(found.port);
            }
        }
        if let Some(reason) = cancelled() {
            return Err(reason);
        }
        if Instant::now() >= deadline {
            if fallback != 0 {
                return Ok(fallback);
            }
            return Err(format!(
                "等待引擎上报端口超时（{} 秒）：进程未退出，但既没有 stdout hello 也没有可用的 port.json",
                timeout.as_secs()
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// 单次 GET /api/health，带 Bearer。只读状态行，够用且不需要引入 HTTP crate。
pub fn health_once(port: u16, token: &str) -> Result<(), String> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(800))
        .map_err(|e| format!("连接 127.0.0.1:{port} 失败：{e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| format!("设置读超时失败：{e}"))?;
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| format!("设置写超时失败：{e}"))?;
    let request = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("发送健康检查失败：{e}"))?;

    let mut buf = [0u8; 512];
    let n = stream
        .read(&mut buf)
        .map_err(|e| format!("读取健康检查响应失败：{e}"))?;
    let head = String::from_utf8_lossy(&buf[..n]);
    let first = head.lines().next().unwrap_or("").trim().to_string();
    let code = first.split_whitespace().nth(1).unwrap_or("");
    if code == "200" {
        Ok(())
    } else {
        Err(format!("health 返回 {}", if first.is_empty() { "空响应" } else { &first }))
    }
}

/// 轮询健康检查。`cancel` 返回 Some(原因) 时立即中止（窗口关闭/子进程提前退出/被重启接管）。
pub fn wait_for_health(
    port: u16,
    token: &str,
    timeout: Duration,
    mut cancel: impl FnMut() -> Option<String>,
) -> Result<(), String> {
    let start = Instant::now();
    let mut last = String::from("尚未收到响应");
    while start.elapsed() < timeout {
        if let Some(reason) = cancel() {
            return Err(reason);
        }
        match health_once(port, token) {
            Ok(()) => return Ok(()),
            Err(e) => last = e,
        }
        std::thread::sleep(Duration::from_millis(400));
    }
    Err(format!(
        "等待引擎健康检查超时（{} 秒）：{}",
        timeout.as_secs(),
        last
    ))
}

/// 读取日志末尾 `max_lines` 行；只读最后 128 KiB，避免大日志撑爆内存。
pub fn tail_lines(path: &Path, max_lines: usize) -> Result<String, String> {
    let mut file = File::open(path).map_err(|e| format!("无法读取日志 {}：{e}", path.display()))?;
    let len = file.metadata().map(|m| m.len()).unwrap_or(0);
    let window = 128 * 1024u64;
    let start = len.saturating_sub(window);
    file.seek(SeekFrom::Start(start))
        .map_err(|e| format!("定位日志失败：{e}"))?;
    let mut buf = Vec::with_capacity((len - start) as usize);
    file.take(window)
        .read_to_end(&mut buf)
        .map_err(|e| format!("读取日志失败：{e}"))?;
    let text = String::from_utf8_lossy(&buf);
    let mut lines: Vec<&str> = text.lines().collect();
    if start > 0 && !lines.is_empty() {
        lines.remove(0); // 第一行可能被截断
    }
    let from = lines.len().saturating_sub(max_lines);
    Ok(lines[from..].join("\n"))
}

/// 读取日志末尾至多 `max_bytes` 字节（给 `engine_status.log_tail` 用）。
/// 从文件中间起读时丢掉被截断的首行；文件不存在/读不了就是空串，绝不 panic。
pub fn tail_bytes(path: &Path, max_bytes: usize) -> String {
    let Ok(mut file) = File::open(path) else {
        return String::new();
    };
    let Ok(meta) = file.metadata() else {
        return String::new();
    };
    let window = max_bytes as u64;
    let start = meta.len().saturating_sub(window);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut buf = Vec::with_capacity(window as usize);
    let _ = file.take(window).read_to_end(&mut buf);
    let text = String::from_utf8_lossy(&buf).to_string();
    if start > 0 {
        if let Some(idx) = text.find('\n') {
            return text[idx + 1..].to_string();
        }
        return String::new();
    }
    text
}

/// taskkill /T /F 兜底，连 Python 派生的 ffmpeg 等子进程一起结束。
pub fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        let mut cmd = Command::new("taskkill");
        cmd.arg("/PID")
            .arg(pid.to_string())
            .arg("/T")
            .arg("/F")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW);
        let _ = cmd.status();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("kill")
            .arg("-9")
            .arg(pid.to_string())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

/// 写引擎状态 + 追加一行 supervisor.log。
/// 状态写独立的 supervisor.log，绝不回写 engine.log，避免“日志尾部又被追加到日志”的递归膨胀。
pub fn set_phase(app: &AppHandle, phase: &str, message: &str, pid: Option<u32>, exit_code: Option<i32>) {
    if let Some(engine) = app.try_state::<EngineState>() {
        if let Ok(mut status) = engine.status.lock() {
            *status = EngineStatus {
                phase: phase.to_string(),
                message: message.to_string(),
                pid,
                exit_code,
            };
        }
        let supervisor_log = engine.log_path.with_file_name("supervisor.log");
        if let Ok(mut log) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&supervisor_log)
        {
            let _ = writeln!(
                log,
                "=== [{}] phase={} pid={:?} exit={:?} {} ===",
                unix_seconds(),
                phase,
                pid,
                exit_code,
                message.replace('\n', " | ")
            );
        }
    }
}

/// `engine_status` 的组装：状态 + 日志尾部（至多 4 KiB）。
pub fn status_report(state: &EngineState) -> EngineStatusReport {
    let status = state
        .status
        .lock()
        .map(|status| status.clone())
        .unwrap_or_else(|_| EngineStatus::new("failed", "引擎状态锁被污染，请重启应用"));
    let error = if status.phase == "failed" || status.phase == "exited" {
        Some(status.message.clone())
    } else {
        None
    };
    // Python 的 RotatingFileHandler 写 engine.log，子进程 stdout/stderr 写
    // engine.out.log；初始化阶段的 ImportError 只在后者里，旧实现永远读不到。
    let out_log = state.log_path.with_file_name("engine.out.log");
    let tail_path = if out_log.metadata().map(|m| m.len() > 0).unwrap_or(false) {
        &out_log
    } else {
        &state.log_path
    };
    EngineStatusReport {
        phase: status.phase.clone(),
        pid: status.pid,
        exit_code: status.exit_code,
        log_tail: tail_bytes(tail_path, LOG_TAIL_LIMIT),
        error,
    }
}

fn epoch_is_current(app: &AppHandle, epoch: u64) -> bool {
    app.try_state::<EngineState>()
        .map(|engine| engine.epoch.load(Ordering::SeqCst) == epoch)
        .unwrap_or(false)
}

/// 监督线程的取消条件：被重启/关闭接管、进程提前退出、状态锁损坏。
fn cancelled(app: &AppHandle, epoch: u64) -> Option<String> {
    let engine = app.try_state::<EngineState>()?;
    if engine.epoch.load(Ordering::SeqCst) != epoch {
        return Some("引擎启动已被重启取代".to_string());
    }
    if engine.stopping.load(Ordering::SeqCst) {
        return Some("引擎启动已取消".to_string());
    }
    let mut slot = match engine.child.lock() {
        Ok(slot) => slot,
        Err(_) => return Some("引擎状态锁被污染".to_string()),
    };
    match slot.as_mut() {
        None => Some("引擎进程未启动".to_string()),
        Some(child) => match child.try_wait() {
            Ok(Some(status)) => Some(format!("引擎进程提前退出（{status}）")),
            Ok(None) => None,
            Err(e) => Some(format!("无法读取引擎进程状态：{e}")),
        },
    }
}

/// 收掉引擎进程与作业对象；可重复调用。作业对象的 `KILL_ON_JOB_CLOSE` 兜底收走 ffmpeg 孙进程。
fn stop_child(engine: &EngineState) {
    // Close the job first: KILL_ON_JOB_CLOSE takes the whole tree (python +
    // ffmpeg) down even when taskkill is unavailable.  taskkill stays as the
    // fallback for the case where AssignProcessToJobObject failed at spawn.
    if let Ok(mut slot) = engine.job.lock() {
        *slot = None;
    }
    let child = engine.child.lock().ok().and_then(|mut slot| slot.take());
    if let Some(mut child) = child {
        let pid = child.id();
        kill_tree(pid);
        let _ = child.kill();
        // Bounded reap: a wedged process must not block shutdown/restart
        // forever (the old code called wait() with no deadline).
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut reaped = false;
        while Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(_)) => {
                    reaped = true;
                    break;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
        if !reaped {
            append_note(&engine.log_path, "stop: 引擎进程 5 秒内未退出，已关闭句柄不再等待");
        }
    }
}

/// 同一代监督线程才允许写状态：epoch 校验与写入放在 restart_lock 内，
/// 避免旧代线程在 restart 之后又把 starting/ready 写回去。
fn set_phase_checked(
    app: &AppHandle,
    epoch: u64,
    phase: &str,
    message: &str,
    pid: Option<u32>,
    exit_code: Option<i32>,
) {
    let Some(engine) = app.try_state::<EngineState>() else {
        return;
    };
    let Ok(_guard) = engine.restart_lock.lock() else {
        return;
    };
    if engine.epoch.load(Ordering::SeqCst) != epoch {
        return;
    }
    set_phase(app, phase, message, pid, exit_code);
}

/// 就绪后的轻量看护：每 2 秒 try_wait 一次，进程没了就转 failed/exited
/// 并收掉作业对象（Job Object 的 KILL_ON_JOB_CLOSE 带走 ffmpeg 孙进程）。
fn spawn_watchdog(app: AppHandle, epoch: u64, pid: u32) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(2));
        if !epoch_is_current(&app, epoch) {
            return;
        }
        let Some(engine) = app.try_state::<EngineState>() else {
            return;
        };
        if engine.stopping.load(Ordering::SeqCst) {
            return;
        }
        let exited = {
            let mut guard = match engine.child.lock() {
                Ok(guard) => guard,
                Err(_) => return,
            };
            match guard.as_mut() {
                None => return,
                Some(child) => match child.try_wait() {
                    Ok(Some(status)) => Some(status.to_string()),
                    Ok(None) => None,
                    Err(e) => Some(format!("无法读取引擎进程状态：{e}")),
                },
            }
        };
        if let Some(reason) = exited {
            if epoch_is_current(&app, epoch) {
                fail(&app, epoch, format!("引擎进程（pid {pid}）已退出：{reason}"));
            }
            return;
        }
    });
}

/// 失败收尾：先判断进程是否已退出（区分 failed / exited），再收进程、写状态。
fn fail(app: &AppHandle, epoch: u64, message: String) {
    let Some(engine) = app.try_state::<EngineState>() else {
        return;
    };
    // Hold the lifecycle lock across "check epoch -> collect child -> write
    // state": without it a restart could adopt a new child between the check
    // and stop_child(), and the failure path would kill the new generation.
    let Ok(_guard) = engine.restart_lock.lock() else {
        return;
    };
    if engine.epoch.load(Ordering::SeqCst) != epoch {
        return;
    }
    let (pid, exit_code) = {
        let mut guard = engine.child.lock().ok();
        let (mut pid, mut code) = (None, None);
        if let Some(slot) = guard.as_mut() {
            if let Some(child) = slot.as_mut() {
                pid = Some(child.id());
                if let Ok(Some(status)) = child.try_wait() {
                    code = status.code();
                }
            }
        }
        (pid, code)
    };
    // 这一代已经失败：先清端口再收进程，engine_info 不会再把一个正在退出的
    // 端口递给前端（已持 restart_lock，别的代次不能写这个字段）。
    engine.port.store(0, Ordering::SeqCst);
    stop_child(&engine);
    if engine.epoch.load(Ordering::SeqCst) != epoch {
        return;
    }
    let phase = if exit_code.is_some() { "exited" } else { "failed" };
    set_phase(app, phase, &message, pid, exit_code);
}

/// 把刚拉起的子进程/作业对象交给托管状态。
/// 在 restart_lock 保护下做「recheck epoch + 落槽」，避免刚检查完 epoch 就被 restart 收走旧进程、
/// 结果这轮的新进程没人收（留下孤儿 python/ffmpeg）。未被接管时调用方需自己收掉进程。
fn adopt_child(app: &AppHandle, epoch: u64, child: Child, job: Option<win32::JobObject>) -> bool {
    let Some(engine) = app.try_state::<EngineState>() else {
        let mut child = child;
        let pid = child.id();
        kill_tree(pid);
        let _ = child.kill();
        let _ = child.wait();
        return false;
    };
    let guard = engine.restart_lock.lock();
    if guard.is_err() || engine.epoch.load(Ordering::SeqCst) != epoch {
        // 被 restart/shutdown 接管：立刻收掉，别留孤儿。
        let mut child = child;
        let pid = child.id();
        kill_tree(pid);
        let _ = child.kill();
        let _ = child.wait();
        return false;
    }
    match engine.child.lock() {
        Ok(mut slot) => *slot = Some(child),
        Err(_) => {
            let mut child = child;
            let pid = child.id();
            kill_tree(pid);
            let _ = child.kill();
            let _ = child.wait();
            return false;
        }
    }
    if let Ok(mut slot) = engine.job.lock() {
        *slot = job;
    } else {
        // 作业对象存不进状态：宁可有意泄漏句柄，也绝不能让 Drop 的 KILL_ON_JOB_CLOSE 立刻杀掉刚拉起的引擎。
        std::mem::forget(job);
    }
    drop(guard);
    true
}

/// 退出时结束引擎整棵进程树（含 ffmpeg 孙进程），并删掉 port.json；可重复调用。
pub fn shutdown(app: &AppHandle) {
    let Some(engine) = app.try_state::<EngineState>() else {
        return;
    };
    // Serialise with restart/adopt: otherwise shutdown could take the child
    // slot just before adopt_child stores a freshly spawned process, leaving
    // an orphan python/ffmpeg behind.  stopping is an irreversible terminal
    // state: restart() refuses to start again once it is set.
    let _guard = engine.restart_lock.lock().ok();
    engine.stopping.store(true, Ordering::SeqCst);
    engine.epoch.fetch_add(1, Ordering::SeqCst);
    engine.port.store(0, Ordering::SeqCst);
    stop_child(&engine);
    // 引擎 try/finally 会删一次；崩溃/强杀时删不掉，壳再删一次。
    let _ = std::fs::remove_file(&engine.port_file);
}

/// 幂等重启：先杀旧进程（含作业对象里的 ffmpeg）再拉起；重复调用只会重启最新一轮。
/// 返回 false 只表示“这次没法重启”（配置解析失败/状态锁损坏），具体原因写在 engine_status。
pub fn restart(app: &AppHandle) -> bool {
    let Some(engine) = app.try_state::<EngineState>() else {
        return false;
    };
    let Ok(_guard) = engine.restart_lock.lock() else {
        return false;
    };
    if engine.stopping.load(Ordering::SeqCst) {
        // 应用正在关闭：不再拉起新进程（shutdown 是不可逆终态）。
        return false;
    }
    if engine.token.is_empty() {
        // CSPRNG 当初就没取到 token：拉起引擎也只会因缺 token 退出（契约退出码 2）。
        set_phase(
            app,
            "failed",
            "无法生成访问令牌（CSPRNG 不可用），请重启应用",
            None,
            None,
        );
        return false;
    }

    // 1) 作废旧监督线程 + 停掉旧进程（epoch 变化就足以让旧 supervisor
    // 的 cancelled() 返回“被重启取代”，不再翻转 stopping）。
    engine.epoch.fetch_add(1, Ordering::SeqCst);
    // 先清端口再收进程：stop_child 最长等 5 秒，这期间 engine_info 不能继续
    // 回旧端口，否则前端会拿旧端口去连并等到健康检查超时（1.3.2 的重启回归）。
    engine.port.store(0, Ordering::SeqCst);
    stop_child(&engine);

    // 2) 清掉上一轮的 port.json，避免读到旧端口
    let _ = std::fs::remove_file(&engine.port_file);

    // 3) 重新解析配置（用户可能刚把路径改好）
    let config = match EngineConfig::resolve() {
        Ok(config) => {
            if let Ok(mut slot) = engine.config.lock() {
                *slot = Some(config.clone());
            }
            config
        }
        Err(e) => {
            set_phase(app, "failed", &format!("无法定位转录引擎：{e}"), None, None);
            return false;
        }
    };

    set_phase(app, "starting", "正在重启转录引擎…", None, None);
    let epoch = engine.epoch.load(Ordering::SeqCst);
    // 重启也由引擎重新 bind(0)：旧端口可能已被别的进程占用。
    let port = 0u16;
    let token = engine.token.clone();
    let handle = app.clone();
    std::thread::spawn(move || supervise(handle, config, port, token, epoch));
    true
}

/// 后台线程：启动 → 存 Child/Job → 发现端口 → 健康轮询 → 更新状态。
/// `epoch` 用于识别“这一轮已被 restart/shutdown 取代”，避免旧线程覆盖新状态。
pub fn supervise(app: AppHandle, config: EngineConfig, port: u16, token: String, epoch: u64) {
    let started = Instant::now();
    if !epoch_is_current(&app, epoch) {
        return;
    }
    set_phase_checked(&app, epoch, "starting", "正在启动转录引擎…", None, None);
    // 上次崩溃可能留下 port.json：先删，免得把旧端口当本轮真实端口。
    // 删之前再确认一次代次，避免把刚被 restart 拉起的新引擎写的 port.json 删掉。
    if epoch_is_current(&app, epoch) {
        let _ = std::fs::remove_file(&port_file_path(&config.data_dir));
    }

    let spawned = match spawn_engine(&config, port, &token) {
        Ok(spawned) => spawned,
        Err(e) => {
            fail(&app, epoch, e);
            return;
        }
    };
    let pid = spawned.child.id();
    let stdout_port = Arc::clone(&spawned.stdout_port);
    let port_file = port_file_path(&config.data_dir);

    if !adopt_child(&app, epoch, spawned.child, spawned.job) {
        return;
    }

    set_phase_checked(
        &app,
        epoch,
        "starting",
        &format!("正在启动转录引擎（pid {pid}）…"),
        Some(pid),
        None,
    );

    let result = match discover_port(&stdout_port, &port_file, pid, port, || {
        cancelled(&app, epoch)
    }) {
        Ok(resolved) => {
            if let Some(engine) = app.try_state::<EngineState>() {
                engine.port.store(resolved, Ordering::SeqCst);
            }
            // 端口发现与健康检查共用同一个 180 秒启动预算：冷启动时导入依赖
            // 就可能花掉十几秒，不能两段各等满 180 秒再报错（启动页也会先超时）。
            let remaining = HEALTH_TIMEOUT
                .saturating_sub(started.elapsed())
                .max(Duration::from_secs(15));
            wait_for_health(resolved, &token, remaining, || cancelled(&app, epoch))
        }
        Err(reason) => Err(reason),
    };

    match result {
        Ok(()) => {
            if epoch_is_current(&app, epoch) {
                let resolved = app
                    .try_state::<EngineState>()
                    .map(|engine| engine.port.load(Ordering::SeqCst))
                    .unwrap_or(port);
                set_phase_checked(
                    &app,
                    epoch,
                    "ready",
                    &format!("引擎已就绪（{} 模式，127.0.0.1:{}）", config.mode, resolved),
                    Some(pid),
                    None,
                );
                // A ready engine used to be unsupervised: a later Python crash
                // kept reporting ready until the user restarted it.
                spawn_watchdog(app.clone(), epoch, pid);
            }
        }
        Err(e) => fail(&app, epoch, e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch_dir(tag: &str) -> PathBuf {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp")
            .join(format!("unit-{tag}-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        dir
    }

    #[test]
    fn token_is_32_lowercase_hex() {
        let token = generate_token().expect("CSPRNG 应当可用");
        assert_eq!(token.len(), 32, "token 必须是 32 位 hex");
        assert!(
            token
                .chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
            "token 只能是小写 hex：{token}"
        );
        assert_ne!(token, generate_token().expect("CSPRNG 应当可用"));
    }

    #[test]
    fn port_file_without_token_does_not_fail() {
        let parsed = parse_port_file(r#"{"port":5000}"#).expect("没有 token 的 port.json 必须能解析");
        assert_eq!(parsed.port, 5000);
        // 旧格式（带 type/token）多余字段忽略，token 不是必需项。
        let legacy = parse_port_file(r#"{"type":"ready","port":5001,"token":"deadbeef","version":"1.2.0","pid":9}"#)
            .expect("旧格式 port.json 必须向后兼容");
        assert_eq!(legacy.port, 5001);
        assert_eq!(legacy.version, "1.2.0");
        assert_eq!(legacy.pid, 9);
        // 坏输入只返回 Err，不 panic。
        assert!(parse_port_file("").is_err());
        assert!(parse_port_file("not-json").is_err());
        assert!(parse_port_file(r#"{"token":"deadbeef"}"#).is_err());
        assert!(parse_port_file(r#"{"port":0}"#).is_err());
        assert!(read_port_file(&scratch_dir("missing").join("port.json")).is_err());
    }

    #[test]
    fn discover_port_prefers_stdout_hello() {
        let dir = scratch_dir("discover-stdout");
        let slot = Arc::new(Mutex::new(Some(45678u16)));
        let port_file = dir.join("port.json");
        // 旧进程残留的 port.json 与 stdout hello 冲突时必须信 stdout。
        std::fs::write(&port_file, r#"{"port":40000,"version":"1.3.2","pid":99}"#).unwrap();
        let port = discover_port_with_timeout(
            &slot,
            &port_file,
            1234,
            0,
            Duration::from_millis(200),
            || None,
        )
        .expect("stdout hello 必须优先于 port.json");
        assert_eq!(port, 45678);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn discover_port_reads_own_port_file() {
        let dir = scratch_dir("discover-own");
        let slot = Arc::new(Mutex::new(None));
        let port_file = dir.join("port.json");
        std::fs::write(&port_file, r#"{"port":45679,"version":"1.3.2","pid":4242}"#).unwrap();
        let port = discover_port_with_timeout(
            &slot,
            &port_file,
            4242,
            0,
            Duration::from_millis(500),
            || None,
        )
        .expect("pid 匹配的 port.json 必须被接受");
        assert_eq!(port, 45679);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn discover_port_rejects_foreign_port_file_and_times_out() {
        let dir = scratch_dir("discover-foreign");
        let slot = Arc::new(Mutex::new(None));
        let port_file = dir.join("port.json");
        std::fs::write(&port_file, r#"{"port":45680,"version":"1.3.2","pid":9}"#).unwrap();
        let started = Instant::now();
        let err = discover_port_with_timeout(
            &slot,
            &port_file,
            4243,
            0,
            Duration::from_millis(250),
            || None,
        )
        .expect_err("别人的 port.json 不能当成本轮端口");
        assert!(started.elapsed() >= Duration::from_millis(200));
        // 关键回归：超时必须 Err，不能把 fallback=0 当端口交给健康检查。
        assert!(err.contains("超时"), "错误信息应说明超时：{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn discover_port_reports_process_death_immediately() {
        let dir = scratch_dir("discover-dead");
        let slot = Arc::new(Mutex::new(None));
        let started = Instant::now();
        let err = discover_port_with_timeout(
            &slot,
            &dir.join("port.json"),
            1,
            0,
            Duration::from_secs(5),
            || Some("引擎进程提前退出（exit status: 1）".to_string()),
        )
        .expect_err("进程早退必须立刻失败");
        assert!(started.elapsed() < Duration::from_secs(2), "不能等满超时");
        assert!(err.contains("提前退出"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 真引擎自 bind(0) 的端到端回归：启动脚本必须能从 stdout 拿到端口并按该端口
    /// 通过健康检查。这正是 1.3.2「首次启动必失败、重试立即成功」的另一半根因
    /// （壳传 --port 0，旧实现 5 秒拿不到端口就把 0 当端口去健康检查）。
    /// 没有开发 venv 的机器（CI/发布机）静默跳过。
    #[test]
    fn real_engine_reports_port_and_answers_health() {
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let engine_dir = manifest.join("..").join("..").join("engine");
        let python = engine_dir
            .join(".venv")
            .join("Scripts")
            .join("python.exe");
        if !python.is_file() || !engine_dir.join("voicetrace_engine").is_dir() {
            eprintln!("skip: 开发 venv/引擎源码不存在（{}）", engine_dir.display());
            return;
        }
        let dir = scratch_dir("real-engine");
        // 启动阶段只要求工具路径存在，不会调用它们；放两个空文件，
        // 测试就不依赖机器 PATH 上是否装了 ffmpeg。
        let bin_dir = dir.join("bin");
        std::fs::create_dir_all(&bin_dir).expect("创建 bin");
        std::fs::write(bin_dir.join("ffmpeg.exe"), b"").expect("占位 ffmpeg");
        std::fs::write(bin_dir.join("ffprobe.exe"), b"").expect("占位 ffprobe");
        let token = generate_token().expect("CSPRNG 应当可用");
        let child_log = dir.join("engine.out.log");
        let log = std::fs::File::create(&child_log).expect("创建引擎日志");
        let mut child = Command::new(&python)
            .arg("-m")
            .arg("voicetrace_engine")
            .arg("--port")
            .arg("0")
            .arg("--data-dir")
            .arg(&dir)
            .arg("--models-dir")
            .arg(dir.join("models"))
            .arg("--ffmpeg")
            .arg(&bin_dir)
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .current_dir(&engine_dir)
            .env("PYTHONPATH", &engine_dir)
            .env("VT_TOKEN", &token)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(log))
            .spawn()
            .expect("应当能拉起开发 venv 里的引擎");
        let stdout_port = pump_stdout(child.stdout.take(), &child_log, &token);
        let port_file = dir.join("port.json");
        let port = discover_port_with_timeout(
            &stdout_port,
            &port_file,
            child.id(),
            0,
            Duration::from_secs(60),
            || match child.try_wait() {
                Ok(Some(status)) => Some(format!("引擎提前退出：{status}")),
                _ => None,
            },
        )
        .expect("引擎必须通过 stdout/port.json 上报自 bind 的端口");
        assert!(port > 0, "端口不能是 0");
        // 引擎在 server.run 之前就打印端口，uvicorn 开始服务可能晚几百毫秒。
        let health = wait_for_health(port, &token, Duration::from_secs(60), || {
            match child.try_wait() {
                Ok(Some(status)) => Some(format!("引擎提前退出：{status}")),
                _ => None,
            }
        });
        kill_tree(child.id());
        let _ = child.wait();
        let _ = std::fs::remove_dir_all(&dir);
        assert!(health.is_ok(), "健康检查失败：{health:?}");
    }

    #[test]
    fn tail_bytes_caps_at_limit_and_keeps_whole_lines() {
        let dir = scratch_dir("tail-bytes");
        let path = dir.join("engine.log");
        let mut text = String::new();
        for i in 0..2000 {
            text.push_str(&format!("line-{i:04}\n"));
        }
        std::fs::write(&path, &text).expect("写测试日志");
        let tail = tail_bytes(&path, 256);
        assert!(tail.len() <= 256, "tail_bytes 必须在 256 字节以内：{}", tail.len());
        assert!(!tail.is_empty());
        assert!(tail.lines().all(|line| line.starts_with("line-")));
        assert!(tail_bytes(&dir.join("absent.log"), 256).is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
