"""Supervise model downloads started from the desktop UI.

The actual transfer runs in a child process (``python -m
voicetrace_engine.model_download``) so "cancel" really stops the network work
instead of waiting for a multi-gigabyte file to finish.  This module only keeps
the process handle and derives progress from the files on disk.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .model_download import DEFAULT_ORDER, EXPECTED_BYTES, LABELS, REPOS, is_model_ready

_CREATE_NO_WINDOW = 0x0800_0000
_KEYS = ("aed", "vad", "punc")


class DownloadManager:
    def __init__(self, models_dir: Path, data_dir: Path, engine_root: Path | None = None) -> None:
        self.models_dir = Path(models_dir)
        self.log_path = Path(data_dir) / "logs" / "model-download.log"
        self.engine_root = Path(engine_root) if engine_root else Path(__file__).resolve().parent.parent

        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._cancel_requested = False
        self._state = "idle"
        self._error: str | None = None
        self._current: str | None = None
        self._repos: list[str] = []
        self._started_at = 0.0
        self._finished_at = 0.0

    # ---------------------------------------------------------------- helpers
    def _dir_bytes(self, key: str) -> int:
        base = self.models_dir / key
        total = 0
        try:
            for root, _dirs, files in os.walk(base):
                for name in files:
                    try:
                        total += (Path(root) / name).stat().st_size
                    except OSError:
                        continue
        except OSError:
            return 0
        return total

    def _tail(self, limit: int = 1200) -> str:
        try:
            size = self.log_path.stat().st_size
            with self.log_path.open("rb") as fh:
                fh.seek(max(0, size - limit))
                text = fh.read().decode("utf-8", "replace").strip()
            return text[-limit:]
        except OSError:
            return ""

    def _model_states(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key in _KEYS:
            expected = EXPECTED_BYTES[key]
            got = min(self._dir_bytes(key), expected)
            out[key] = {
                "label": LABELS[key],
                "ready": is_model_ready(self.models_dir, key),
                "bytes": got,
                "total": expected,
            }
        return out

    # ------------------------------------------------------------------状态
    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            current = self._current
            error = self._error
            started_at = self._started_at
            finished_at = self._finished_at

        models = self._model_states()
        downloaded = sum(item["bytes"] for item in models.values())
        total = sum(item["total"] for item in models.values())
        progress = 1.0 if state == "done" else (0.0 if total <= 0 else min(1.0, downloaded / total))
        if current is None:
            current = next((k for k in _KEYS if not models[k]["ready"]), None)
        return {
            "state": state,
            "current": current,
            "progress": round(progress, 4),
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "error": error,
            "started_at": started_at,
            "finished_at": finished_at,
            "repos": list(self._repos),
            "models": models,
        }

    # ---------------------------------------------------------------- actions
    def start(
        self,
        repos: list[str] | None = None,
        proxy: str = "",
        endpoint: str = "",
    ) -> dict[str, Any]:
        # check→spawn→adopt 必须整体原子：旧实现只在开头查一次"是否在跑"，
        # 随后释放锁去 Popen——两个并发的"下载"请求会一起通过检查、各拉一个
        # 子进程写同一个 models 目录。RLock 允许内部再调 status()。
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self.status()

            requested = [key for key in (repos or DEFAULT_ORDER) if key in REPOS]
            if not requested:
                requested = list(DEFAULT_ORDER)
            missing = [key for key in requested if not is_model_ready(self.models_dir, key)]

            if not missing:
                self._state = "done"
                self._error = None
                self._current = None
                self._repos = []
                self._finished_at = time.time()
                return self.status()

            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log = self.log_path.open("a", encoding="utf-8", errors="replace")
            log.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} download "
                f"{','.join(missing)} proxy={proxy or '-'} endpoint={endpoint or '-'} ===\n"
            )
            log.flush()

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(self.engine_root) + (os.pathsep + existing if existing else "")
            cmd = [
                sys.executable,
                "-m",
                "voicetrace_engine.model_download",
                "--models-dir",
                str(self.models_dir),
                "--repos",
                ",".join(missing),
            ]
            if proxy:
                cmd += ["--proxy", proxy]
            if endpoint:
                cmd += ["--endpoint", endpoint]

            creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.engine_root),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            except OSError as exc:
                log.close()
                self._state = "error"
                self._error = f"无法启动下载进程：{exc}"
                return self.status()

            self._proc = proc
            self._cancel_requested = False
            self._state = "running"
            self._error = None
            self._current = missing[0]
            self._repos = missing
            self._started_at = time.time()
            self._finished_at = 0.0

            self._thread = threading.Thread(target=self._monitor, args=(proc, log), daemon=True)
            self._thread.start()
            return self.status()

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            running = proc is not None and proc.poll() is None
            if running:
                self._cancel_requested = True
        if running and proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        return self.status()

    # --------------------------------------------------------------- monitor
    def _monitor(self, proc: subprocess.Popen, log: Any) -> None:
        code: int | None = None
        try:
            while proc.poll() is None:
                time.sleep(1.0)
                with self._lock:
                    if self._proc is not proc:
                        return  # 状态槽已属于新一轮下载，旧监视线程就此退场
                    repos = list(self._repos)
                current = next(
                    (key for key in repos if not is_model_ready(self.models_dir, key)),
                    None,
                )
                with self._lock:
                    if self._proc is not proc:
                        return
                    self._current = current
            code = proc.poll()
        finally:
            try:
                log.close()
            except OSError:
                pass

        with self._lock:
            # 旧进程的监视线程可能在新一轮 start() 之后才醒来：状态槽已经
            # 属于新进程，旧结果绝不能覆盖回去（否则"下载中"会被写成上一轮
            # 的 done/error，进度与按钮状态全部错乱）。
            if self._proc is not proc:
                return
            canceled = self._cancel_requested
            self._state = "canceled" if canceled else ("done" if code == 0 else "error")
            if self._state == "error":
                tail = self._tail()
                self._error = tail or f"下载进程退出码 {code}"
            elif self._state == "done":
                self._error = None
            self._current = None
            self._finished_at = time.time()
