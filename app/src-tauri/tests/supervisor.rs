use std::fs;
use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use voicetrace_lib::engine::{
    generate_token, health_once, kill_tree, parse_port_file, pick_free_port, read_port_file,
    tail_lines, wait_for_health,
};

fn python() -> String {
    std::env::var("VOICETRACE_TEST_PYTHON").unwrap_or_else(|_| "python".to_string())
}

fn scratch_dir(tag: &str) -> PathBuf {
    // 不写系统临时目录：部分受限环境（以及 TEMP=/tmp 的 Git Bash）会拒绝创建目录。
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("test-tmp");
    let dir = base.join(format!(
        "voicetrace-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    ));
    if let Err(e) = fs::create_dir_all(&dir) {
        panic!("create_dir_all({}) failed: {e}", dir.display());
    }
    dir
}

fn write_fake_engine(root: &Path) -> PathBuf {
    let pkg = root.join("voicetrace_engine");
    fs::create_dir_all(&pkg).unwrap();
    // token 只从 VT_TOKEN 环境变量读：契约里禁止再用 --token（进程列表可见）。
    let main = r#"
import http.server, json, os, sys

args = sys.argv[1:]
port = None
for i, a in enumerate(args):
    if a == "--port":
        port = int(args[i + 1])
token = os.environ.get("VT_TOKEN", "")
if port is None or not token:
    raise SystemExit("fake engine: missing --port/VT_TOKEN")

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path.split("?")[0] == "/api/health" and self.headers.get("Authorization") == "Bearer " + token
        body = json.dumps({"ok": ok, "version": "fake", "uptime_s": 0}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
"#;
    fs::write(pkg.join("__main__.py"), main.trim_start()).unwrap();
    // PYTHONPATH 必须指向包所在目录（root），而不是包目录本身。
    root.to_path_buf()
}

#[test]
fn token_is_32_lowercase_hex_and_unique() {
    let a = generate_token().expect("CSPRNG 应当可用");
    let b = generate_token().expect("CSPRNG 应当可用");
    assert_eq!(a.len(), 32);
    assert_eq!(b.len(), 32);
    assert!(a.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    assert_ne!(a, b);
}

#[test]
fn port_json_without_token_parses() {
    let dir = scratch_dir("portfile");
    let path = dir.join("port.json");
    // 新契约：只含 {port, version, pid}，没有 token。
    fs::write(&path, r#"{"port":43117,"version":"1.3.0","pid":4242}"#).unwrap();
    let parsed = read_port_file(&path).expect("没有 token 的 port.json 必须能解析");
    assert_eq!(parsed.port, 43117);
    assert_eq!(parsed.version, "1.3.0");
    assert_eq!(parsed.pid, 4242);
    // 旧版 hello 带 type/token：多余字段忽略，token 不是必需项，也不能因此崩。
    fs::write(
        &path,
        r#"{"type":"ready","port":43118,"token":"deadbeef","version":"1.2.0","pid":7}"#,
    )
    .unwrap();
    assert_eq!(read_port_file(&path).unwrap().port, 43118);
    // 坏输入只返回 Err，不 panic。
    assert!(parse_port_file("not-json").is_err());
    assert!(parse_port_file(r#"{"token":"deadbeef"}"#).is_err());
    assert!(parse_port_file(r#"{"port":0}"#).is_err());
    assert!(read_port_file(&dir.join("absent.json")).is_err());
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn free_port_is_bindable() {
    let port = pick_free_port().unwrap();
    assert!(port > 0);
    let listener = TcpListener::bind(("127.0.0.1", port)).unwrap();
    drop(listener);
}

#[test]
fn tail_lines_reads_only_the_tail() {
    let dir = scratch_dir("tail");
    let log = dir.join("engine.log");
    let mut f = fs::File::create(&log).unwrap();
    for i in 0..120 {
        writeln!(f, "line-{i:03}").unwrap();
    }
    drop(f);
    let tail = tail_lines(&log, 50).unwrap();
    let lines: Vec<&str> = tail.lines().collect();
    assert_eq!(lines.len(), 50);
    assert_eq!(lines[0], "line-070");
    assert_eq!(lines[49], "line-119");
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn fake_engine_spawn_health_and_kill() {
    let dir = scratch_dir("supervisor");
    let pkg_root = write_fake_engine(&dir);
    let python = python();
    let port = pick_free_port().unwrap();
    let token = generate_token().expect("CSPRNG 应当可用");
    let log = dir.join("engine.log");
    let log_file = fs::File::create(&log).unwrap();

    let mut child = Command::new(&python)
        .current_dir(&pkg_root)
        .env("PYTHONPATH", &pkg_root)
        // 契约：token 只走环境变量，命令行不再出现 --token。
        .env("VT_TOKEN", &token)
        .arg("-m")
        .arg("voicetrace_engine")
        .arg("--port")
        .arg(port.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file.try_clone().unwrap()))
        .stderr(Stdio::from(log_file))
        .spawn()
        .expect("python fake engine should start (set VOICETRACE_TEST_PYTHON if needed)");

    let result = wait_for_health(port, &token, Duration::from_secs(15), || {
        if let Ok(Some(status)) = child.try_wait() {
            Some(format!("fake engine exited: {status}"))
        } else {
            None
        }
    });
    assert!(result.is_ok(), "health failed: {result:?}");
    assert!(health_once(port, "wrong-token").is_err(), "wrong token must be rejected");

    kill_tree(child.id());
    let status = child.wait().unwrap();
    println!("fake engine killed, status={status}");

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn health_times_out_against_closed_port() {
    // 绑定后立刻释放，得到一个大概率无人监听的端口。
    let port = pick_free_port().unwrap();
    let _ = TcpStream::connect_timeout(
        &([127, 0, 0, 1], port).into(),
        Duration::from_millis(100),
    );
    let result = wait_for_health(port, "x", Duration::from_millis(1200), || None);
    assert!(result.is_err());
}
