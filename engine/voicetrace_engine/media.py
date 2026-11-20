from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("voicetrace.media")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v",
    ".ts", ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp", ".mts", ".m2ts", ".vob", ".ogv",
}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".ape"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS


@dataclass
class MediaInfo:
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    has_video: bool = False
    size_bytes: int = 0
    format_name: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    bit_rate: int = 0
    probe_failed: bool = False

    def as_dict(self) -> dict:
        return {
            "duration_ms": self.duration_ms, "width": self.width, "height": self.height,
            "has_audio": self.has_audio, "has_video": self.has_video,
            "size_bytes": self.size_bytes, "format_name": self.format_name,
            "video_codec": self.video_codec, "audio_codec": self.audio_codec,
            "bit_rate": self.bit_rate,
        }


def _parse_ts(value: str) -> float:
    """Parse ffmpeg's HH:MM:SS.microseconds progress value into seconds.

    ffmpeg emits out_time_us, out_time_ms and out_time on every -progress
    line; the human-readable one used to raise NameError here, which killed
    the reader thread and made the watchdog report a false stall.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    parts = text.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    return float(text)


def _run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )


def probe(ffprobe: Path, path: Path) -> MediaInfo:
    # exists() + stat() is not atomic: the file may be deleted between them.
    # A vanished file must look like "size unknown", not raise out of probe.
    try:
        size_bytes = path.stat().st_size if path.is_file() else 0
    except OSError:
        size_bytes = 0
    info = MediaInfo(size_bytes=size_bytes)
    try:
        cp = _run([
            str(ffprobe), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ], timeout=120)
        data = json.loads(cp.stdout or "{}")
    except Exception:
        info.probe_failed = True
        return info
    fmt = data.get("format") or {}
    try:
        info.duration_ms = int(round(float(fmt.get("duration", 0)) * 1000))
    except (TypeError, ValueError):
        info.duration_ms = 0
    info.format_name = str(fmt.get("format_name", ""))
    try:
        info.bit_rate = int(fmt.get("bit_rate", 0) or 0)
    except (TypeError, ValueError):
        pass
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.width = int(st.get("width") or 0)
            info.height = int(st.get("height") or 0)
            info.video_codec = str(st.get("codec_name") or "")
            if not info.duration_ms:
                try:
                    info.duration_ms = int(round(float(st.get("duration", 0)) * 1000))
                except (TypeError, ValueError):
                    pass
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = str(st.get("codec_name") or "")
    return info


def _terminate(proc: subprocess.Popen) -> None:
    """Kill a child *and its descendants* without ever blocking the caller.

    taskkill /T matters on Windows: a wrapper (or a future ffmpeg filter graph)
    may own grandchildren that keep inherited handles -- including our stderr
    log file -- alive after the direct child is gone.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass


def extract_audio(ffmpeg: Path, src: Path, dst: Path, duration_ms: int,
                  on_progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None,
                  stall_timeout_s: float = 180.0) -> None:
    """Decode any media to 16 kHz mono PCM s16le wav, with coarse progress.

    Robustness notes (regression: one corrupt file used to hang the only worker
    forever):

    * stderr goes to a side file, never to an undrained pipe -- ffmpeg easily
      writes more than 4 KB of warnings, which deadlocks on Windows when nobody
      reads the other end;
    * stdout is drained by a reader thread while the main thread owns the
      watchdog, so a silent hang is still detected;
    * a stall watchdog (default 180 s without progress) kills the child, and
      cancellation raises Cancelled so the job ends as canceled, not error.
    """
    from .runtime import Cancelled  # local import keeps this module light

    dst.parent.mkdir(parents=True, exist_ok=True)
    err_path = dst.with_name(dst.name + ".ffmpeg.log")
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(src),
        "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        str(dst),
    ]
    total_s = max(duration_ms / 1000.0, 0.001)
    shared = {"last": time.monotonic(), "emit": 0.0, "sec": 0.0}

    def _reader(stream) -> None:
        for raw in stream:
            # A malformed progress line must never kill the reader thread: a
            # dead reader makes the watchdog report a false stall and kill an
            # otherwise healthy decode.
            try:
                shared["last"] = time.monotonic()
                line = raw.strip()
                if not line or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    sec = int(val) / 1_000_000.0
                elif key == "out_time":
                    sec = _parse_ts(val)
                elif key == "progress" and val == "end":
                    sec = total_s
                else:
                    continue
                shared["sec"] = sec
                now = time.monotonic()
                if on_progress and (now - shared["emit"] > 0.35 or sec >= total_s):
                    shared["emit"] = now
                    try:
                        on_progress(max(0.0, min(1.0, sec / total_s)))
                    except Exception:  # noqa: BLE001 - progress must never kill a job
                        pass
            except Exception:  # noqa: BLE001 - keep draining stdout
                log.debug("ignoring malformed ffmpeg progress line", exc_info=True)
                continue

    cancelled = False
    try:
        with open(err_path, "w", encoding="utf-8", errors="replace") as err_file:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err_file,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
            reader = threading.Thread(target=_reader, args=(proc.stdout,), daemon=True)
            reader.start()
            try:
                while True:
                    try:
                        proc.wait(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                            _terminate(proc)
                            raise Cancelled("cancelled")
                        if time.monotonic() - shared["last"] > stall_timeout_s:
                            _terminate(proc)
                            raise RuntimeError(
                                "ffmpeg stalled > %ds at %.1fs/%.1fs"
                                % (int(stall_timeout_s), shared["sec"], total_s)
                            )
            finally:
                if proc.poll() is None:
                    _terminate(proc)
                try:
                    # 必须 reaper 掉子进程：否则它继承的 stderr 句柄会让日志文件锁住
                    proc.wait(timeout=5.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                reader.join(timeout=2.0)
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except (OSError, ValueError):
                    pass
                try:
                    err_file.flush()
                except (OSError, ValueError):
                    pass

        try:
            err = err_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            err = ""
        if proc.returncode not in (0, None) and not cancelled:
            raise RuntimeError("ffmpeg failed (%s): %s" % (proc.returncode, err.strip()[-600:]))
    finally:
        # 成功 / 取消 / 卡死 / 失败都不把 <job>.wav.ffmpeg.log 留在 cache 里：
        # 旧实现只在成功路径删，取消与失败会一路逃逸到 raise 之前，文件永远堆积。
        # Windows 上杀进程后句柄/杀软扫描可能短暂占用文件，退避重试几次。
        for attempt in range(6):
            try:
                err_path.unlink()
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.05 * (attempt + 1))


def make_thumb(ffmpeg: Path, src: Path, dst: Path, at_s: float = 0.0,
               width: int = 480) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-ss", f"{max(at_s, 0):.2f}", "-i", str(src),
        "-frames:v", "1", "-vf", f"scale={width}:-2",
        "-q:v", "4", "-loglevel", "error", str(dst),
    ]
    try:
        cp = _run(cmd, timeout=60)
        return cp.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except Exception:
        return False


def human_duration(ms: int) -> str:
    s = max(0, int(ms // 1000))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"
