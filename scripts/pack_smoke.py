"""便携版端到端冒烟：导入 → 转录 → 导出，只碰 HTTP，不加载模型。

为什么需要显式参数：包里的引擎 token 由壳随机生成（不进 port.json），
所以本脚本面向「手动带 --token 起的引擎」，与 build-release.ps1 无关。

用法（引擎已起好）：
    python pack_smoke.py <视频路径> [--port-file <port.json>] [--token <token>] [--out-dir <目录>]

默认：port 文件按 包\data\port.json → 包\_port.json → ..\..\_runtime\port.json 依次尝试；
token 取环境变量 VT_TOKEN，否则 'rel'（对应手动启动时的 --token rel）。
导出默认进隔离目录 _scratch/<日期>/smoke-out/（临时把引擎 export_dir 指过去，跑完还原），
不传显式文件路径，由引擎做同名序号避让，绝不覆盖视频同目录的用户字幕。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
RELEASE = PROJECT / "#release" / "声迹_portable"
PORT_CANDIDATES = [
    RELEASE / "data" / "port.json",
    RELEASE / "_port.json",
    PROJECT / "_runtime" / "port.json",
]
TERMINAL = ("done", "error", "canceled")


def pick_port_file(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for candidate in PORT_CANDIDATES:
        if candidate.is_file():
            return candidate
    sys.exit(f"找不到 port.json，试过：{[str(p) for p in PORT_CANDIDATES]}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="VoiceTrace 便携版端到端冒烟")
    ap.add_argument("media", help="要导入的视频/音频路径")
    ap.add_argument("--port-file", default=None)
    ap.add_argument("--token", default=os.environ.get("VT_TOKEN", "rel"))
    ap.add_argument("--out-dir", default=None,
                    help="导出目录（默认 _scratch/<日期>/smoke-out/，隔离于视频目录，避免覆盖用户字幕）")
    ap.add_argument("--timeout", type=float, default=1800.0, help="最长等待秒数")
    args = ap.parse_args(argv)

    media = Path(args.media).resolve()
    if not media.is_file():
        return print(f"素材不存在：{media}") or 2
    port_file = pick_port_file(args.port_file)
    port = json.loads(port_file.read_text(encoding="utf-8"))["port"]
    base = f"http://127.0.0.1:{port}/api"
    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"}

    def call(method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:                      # 401 = token 不对，直接说清楚
            sys.exit(f"{method} {path} -> HTTP {e.code}：{e.read()[:200].decode('utf-8', 'replace')}")

    print("port.json:", port_file)
    print("health   :", call("GET", "/health"))

    created = call("POST", "/jobs", {"paths": [str(media)]})
    jobs = created.get("jobs") or []
    if not jobs:
        print(f"导入后没有新任务（可能全部被跳过），无法冒烟：skipped={created.get('skipped')}",
              file=sys.stderr)
        return 2
    job_id = jobs[0]["id"]
    print("job      :", job_id, "| skipped:", created.get("skipped"))

    deadline = time.time() + args.timeout
    job = {}
    while time.time() < deadline:
        job = call("GET", f"/jobs/{job_id}")
        if job["status"] in TERMINAL:
            break
        time.sleep(2)
    print("status   :", job.get("status"), job.get("stage"), job.get("error"))
    if job.get("status") != "done":
        return 1

    out_dir = (Path(args.out_dir) if args.out_dir
               else PROJECT / "_scratch" / time.strftime("%Y-%m-%d") / "smoke-out")
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # 临时把引擎 export_dir 指向隔离目录，导出时不传显式 path：
    # 引擎走 unique_export_path 同名序号避让，不会覆盖任何已有文件；跑完还原设置。
    old_export_dir = call("GET", "/settings").get("export_dir", "")
    call("PUT", "/settings", {"export_dir": str(out_dir)})
    try:
        for fmt in ("srt", "vtt", "ass", "txt", "json"):
            got = call("POST", f"/jobs/{job_id}/export", {"format": fmt})
            print(f"  export {fmt}: {got['path']} ({Path(got['path']).stat().st_size} B)")
    finally:
        call("PUT", "/settings", {"export_dir": old_export_dir})
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
