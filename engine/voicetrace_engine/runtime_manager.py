"""Supervise the on-demand PyTorch runtime installation.

Mirrors :mod:`voicetrace_engine.downloader`: the heavy work happens in a child
process so the UI can cancel it, while this class only keeps the handle and
derives progress from the wheel files on disk.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .runtime_setup import TOTAL_BYTES, WHEELS, installed_ok, torch_ready

_CREATE_NO_WINDOW = 0x0800_0000


def resolve_site_packages(engine_root: Path) -> Path | None:
    """Portable layout: <app>/runtime/site-packages. Dev mode: None."""
    candidate = Path(engine_root).parent / "runtime" / "site-packages"
    return candidate if candidate.is_dir() else None


class RuntimeManager:
    def __init__(self, engine_root: Path, data_dir: Path) -> None:
        self.engine_root = Path(engine_root)
        self.site_packages = resolve_site_packages(self.engine_root)
        self.download_dir = (
            self.site_packages.parent / ".downloads" if self.site_packages else None
        )
        self.log_path = Path(data_dir) / "logs" / "runtime-install.log"

        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._cancel_requested = False
        self._state = "idle"
        self._phase = "idle"
        self._error: str | None = None
        self._started_at = 0.0
        self._finished_at = 0.0

    # ---------------------------------------------------------------- helpers
    def _downloaded_bytes(self) -> int:
        if not self.download_dir or not self.download_dir.is_dir():
            return 0
        total = 0
        for item in WHEELS:
            for name in (item["filename"], item["filename"] + ".part"):
                try:
                    total += (self.download_dir / name).stat().st_size
                except OSError:
                    continue
        return min(total, TOTAL_BYTES)

    def _ready(self) -> bool:
        if self.site_packages and installed_ok(self.site_packages):
            return True
        return torch_ready()

    def _tail(self, limit: int = 1200) -> str:
        try:
            size = self.log_path.stat().st_size
            with self.log_path.open("rb") as fh:
                fh.seek(max(0, size - limit))
                return fh.read().decode("utf-8", "replace").strip()[-limit:]
        except OSError:
            return ""

    # ------------------------------------------------------------------状态
    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            phase = self._phase
            error = self._error
            started_at = self._started_at
            finished_at = self._finished_at

        ready = self._ready()
        downloaded = self._downloaded_bytes()
        if state == "done" or (state == "idle" and ready):
            progress = 1.0
        elif phase == "install":
            progress = 1.0
        else:
            progress = 0.0 if TOTAL_BYTES <= 0 else min(1.0, downloaded / TOTAL_BYTES)
        return {
            "state": state,
            "phase": phase,
            "torch_ready": ready,
            "downloaded_bytes": downloaded,
            "total_bytes": TOTAL_BYTES,
            "progress": round(progress, 4),
            "error": error,
            "started_at": started_at,
            "finished_at": finished_at,
            "supported": self.site_packages is not None,
        }

    # ---------------------------------------------------------------- actions
    def start(self, proxy: str = "", mirror: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self.status()

        if self._ready():
            with self._lock:
                self._state = "done"
                self._phase = "done"
                self._error = None
                self._finished_at = time.time()
            return self.status()

        if self.site_packages is None or self.download_dir is None:
            with self._lock:
                self._state = "error"
                self._error = "当前是源码开发模式，请直接用 pip 安装 requirements.txt"
            return self.status()

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("a", encoding="utf-8", errors="replace")
        log.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} runtime install "
            f"proxy={proxy or '-'} mirror={int(mirror)} ===\n"
        )
        log.flush()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.engine_root) + (os.pathsep + existing if existing else "")
        cmd = [
            sys.executable,
            "-m",
            "voicetrace_engine.runtime_setup",
            "--site-packages",
            str(self.site_packages),
            "--download-dir",
            str(self.download_dir),
        ]
        if proxy:
            cmd += ["--proxy", proxy]
        if mirror:
            cmd.append("--mirror")

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
            with self._lock:
                self._state = "error"
                self._error = f"无法启动安装进程：{exc}"
            return self.status()

        with self._lock:
            self._proc = proc
            self._cancel_requested = False
            self._state = "running"
            self._phase = "download"
            self._error = None
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
        phase = "download"
        try:
            while proc.poll() is None:
                time.sleep(1.0)
                if phase == "download" and self.site_packages is not None:
                    torch_dir = self.site_packages / "torch"
                    if torch_dir.is_dir():
                        phase = "install"
                        with self._lock:
                            self._phase = "install"
                with self._lock:
                    if self._phase != phase:
                        self._phase = phase
        finally:
            try:
                log.close()
            except OSError:
                pass

        code = proc.poll()
        with self._lock:
            canceled = self._cancel_requested
            if canceled:
                self._state = "canceled"
            elif code == 0 and self._ready():
                self._state = "done"
                self._phase = "done"
                self._error = None
            else:
                self._state = "error"
                self._error = self._tail() or f"安装进程退出码 {code}"
            self._finished_at = time.time()
