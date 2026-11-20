"""Local HTTP/WS server for the VoiceTrace engine.

The process is a hidden child of the desktop shell; it binds 127.0.0.1 on a
port chosen by the parent (or an ephemeral port we report on stdout) and
requires a bearer token for everything except /api/health.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import mimetypes
import os
import socket
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterable

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import analysis, cleanup, edit
from . import export as exporter
from .config import VERSION, Paths, load_settings, resolve_paths, save_settings
from .downloader import DownloadManager
from .media import MEDIA_EXTS, probe
from .model_download import is_model_ready
from .runtime_manager import RuntimeManager
from .pipeline import OOM_DOWNGRADE_MESSAGE, OOM_MESSAGE, is_out_of_memory
from .runtime import Runtime, probe_gpu
from .segmentation import SEG_VERSION, segment_transcript
from .store import Store, now_iso
from .textutil import count_chars

log = logging.getLogger("voicetrace.server")

CHUNK = 1024 * 1024
SAMPLE_RATE = 16000

# engine.log is rotated in-process so a long-running engine cannot fill the
# user's disk (the shell also appends stdout/stderr to the same file).
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Windows refuses these names (with or without an extension) as file names.
RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# format -> the only extension the desktop client may export to.
EXPORT_EXTS = {
    "srt": ".srt",
    "vtt": ".vtt",
    "ass": ".ass",
    "txt": ".txt",
    "json": ".json",
}

# The desktop WebView and the local Vite dev server are the only legitimate
# callers.  A wildcard made every page the user opened able to read the API
# (the bearer token still gates it, but the contract is local-only).
ALLOWED_ORIGINS = [
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
]


# ------------------------------------------------------------- small helpers


def resolve_token(args: argparse.Namespace) -> str:
    """Read the bearer token from --token or VT_TOKEN, never invent one.

    A random fallback used to be generated here, which silently produced an
    unreachable engine: the shell kept its own token, every request 401'd and
    the generated token leaked into the log. Failing fast is the only safe
    behaviour.
    """
    token = (getattr(args, "token", "") or "").strip()
    if not token:
        token = (os.environ.get("VT_TOKEN") or "").strip()
    if not token:
        print("engine: missing token", file=sys.stderr)
        sys.exit(2)
    return token


def same_token(given: str, expected: str) -> bool:
    """Constant-time token comparison (bytes keeps non-ASCII safe)."""
    return hmac.compare_digest((given or "").encode("utf-8"),
                               (expected or "").encode("utf-8"))


def query_token_allowed(path: str) -> bool:
    """Only media / thumb / sockets may carry the token in the query string.

    Everywhere else the token must ride in the Authorization header: a token
    in a URL ends up in the WebView cache, Referer headers and crash dumps.
    """
    return path.endswith(("/media", "/thumb")) or path in ("/ws", "/api/ws")


_GPU_INFO: dict[str, Any] = {"at": 0.0, "info": None}
GPU_INFO_TTL_S = 5.0


def gpu_snapshot() -> dict:
    """probe_gpu with a short TTL cache.

    /system is polled and the websocket snapshot is built on the event loop,
    so the (first, slow) torch probe must not run once per request."""
    cached = _GPU_INFO.get("info")
    now = time.time()
    if cached is not None and now - float(_GPU_INFO.get("at") or 0.0) < GPU_INFO_TTL_S:
        return dict(cached)
    info = probe_gpu()
    _GPU_INFO["at"] = now
    _GPU_INFO["info"] = info
    return dict(info)


def model_state(runtime: Any) -> str:
    """Map the runtime flags onto the documented idle/loading/ready/failed."""
    if getattr(runtime, "loading", False):
        return "loading"
    if getattr(runtime, "ready", False):
        return "ready"
    if getattr(runtime, "error", None):
        return "failed"
    return "idle"


def is_reserved_device_name(name: str) -> bool:
    """True for CON/PRN/AUX/NUL/COM1-9/LPT1-9 (stem before the first dot)."""
    stem = (name or "").split(".")[0].strip().rstrip(" ").upper()
    return stem in RESERVED_DEVICE_NAMES


def unique_export_path(base_dir: Path, name: str) -> Path:
    """Never silently overwrite an earlier export with the same basename."""
    candidate = Path(base_dir) / name
    if not candidate.exists():
        return candidate
    stem, ext = os.path.splitext(name)
    for index in range(2, 1000):
        candidate = Path(base_dir) / f"{stem} ({index}){ext}"
        if not candidate.exists():
            return candidate
    return Path(base_dir) / f"{stem} ({time.time_ns()}){ext}"


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    """Write through a sibling temp file + os.replace (never half-written)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return path


def thumb_file(thumb_dir: Path, job_id: str) -> Path:
    """Resolve <job_id>.jpg and refuse anything that escapes thumb_dir.

    The id is still validated against the store by the endpoint; this guard is
    the second line of defence for ids that reached us before that check
    (e.g. a Windows ..\\..\\x path segment).
    """
    base = Path(thumb_dir).resolve()
    candidate = (base / f"{job_id}.jpg").resolve()
    if candidate.parent != base:
        raise HTTPException(404, "暂无封面")
    return candidate


def check_version(transcript: dict, expected: int | None) -> None:
    """Optimistic concurrency: only enforced when the client sends a version."""
    if expected is None:
        return
    current = int(transcript.get("version") or 1)
    if current != int(expected):
        raise HTTPException(409, f"版本冲突：当前 {current}，请求 {expected}")


# ---------------------------------------------------------------- app state


class Hub:
    """Fan-out of JSON events to websocket clients (thread safe)."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def connect(self, ws: WebSocket, snapshot: Callable[[], dict] | None = None) -> None:
        """Accept, join the fan-out, then push the state snapshot.

        The old order (send the snapshot, then clients.add) dropped every event
        published between those two steps: a job that started or finished in
        that window stayed at its previous state until the next event arrived.
        Registering first closes the window; an incremental frame that overtakes
        the snapshot is harmless because the desktop client upserts jobs by id
        and the snapshot built right after is authoritative.
        """
        await ws.accept()
        with self._lock:
            self.clients.add(ws)
        if snapshot is not None:
            await ws.send_json(snapshot())

    def disconnect(self, ws: WebSocket) -> None:
        with self._lock:
            self.clients.discard(ws)

    def publish(self, event: dict) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, event)
        except RuntimeError:
            pass

    def _fanout(self, event: dict) -> None:
        with self._lock:
            clients = list(self.clients)
        if not clients:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - fan-out always runs in the loop
            return
        for ws in clients:
            # A send that raises must not surface as an unhandled task
            # exception (asyncio logs those loudly and keeps the client).
            task = loop.create_task(self._send(ws, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _send(self, ws: WebSocket, event: dict) -> None:
        try:
            await ws.send_json(event)
        except Exception:  # noqa: BLE001 - a dead client is not an error
            self.disconnect(ws)


class State:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.token = resolve_token(args)
        self.paths: Paths = resolve_paths(args)
        self.paths.ensure()
        self.models = DownloadManager(
            self.paths.models_dir,
            self.paths.data_dir,
            engine_root=self.paths.engine_root,
        )
        self.runtime_setup = RuntimeManager(self.paths.engine_root, self.paths.data_dir)
        self.settings = load_settings(self.paths.settings_path)
        self.store = Store(self.paths.db_path)
        self.store.reset_stale()
        self._sweep_orphans()
        self.hub = Hub()
        # One lock serialises the job lifecycle (claim / cancel / retry /
        # delete / final commit), so a cancel can never be overwritten by a
        # worker that dequeued the job just before the status update.
        self.lifecycle_lock = threading.RLock()
        self.settings_lock = threading.Lock()
        self.cancel_requested: set[str] = set()
        self.deleted: set[str] = set()
        # One reentrant lock serialises every read-modify-write of a transcript
        # (manual edit, bulk apply, undo and the legacy segment migration), so
        # two requests can never overwrite each other's sentences.
        #
        # It must exist *before* the pipeline is built and be handed to it:
        # pipeline._save_transcript used to get its own private RLock, so the
        # worker's final save and an editor's read-modify-write could both read
        # version N and both write N+1 for different content (a lost edit).
        self.transcript_lock = threading.RLock()
        self.runtime = Runtime(self.paths, self.settings, use_gpu=args.gpu)
        from .pipeline import Pipeline

        self.pipeline = Pipeline(
            self.paths, self.store, self.settings, self.runtime,
            emit=self.hub.publish,
            lifecycle_lock=self.lifecycle_lock,
            transcript_lock=self.transcript_lock,
            cancel_requested=self.cancel_requested,
            deleted=self.deleted,
        )
        # B2：显存不足时的自动降级重试只在本进程内存里计数（不加数据库列）。
        # 进程重启后每个任务允许再自动重试一次——最坏只是多重跑一遍，无害。
        self.worker = Worker(self)
        self.started = time.time()
        self.ffmpeg = self.paths.ffmpeg()
        self.ffprobe = self.paths.ffprobe()

    # ------------------------------------------------------------------
    def _sweep_orphans(self) -> None:
        """Remove sidecars whose job row no longer exists.

        A delete during an ASR batch used to leave partial/<id>.json behind
        with no DB row; startup is the safe place to clean those up.
        """
        try:
            live = {job["id"] for job in self.store.list_jobs()}
        except Exception:  # noqa: BLE001 - a broken DB is handled elsewhere
            return
        partial = self.paths.data_dir / "partial"
        try:
            for path in partial.glob("*.json"):
                if path.stem not in live:
                    try:
                        path.unlink()
                        log.info("已清理孤儿中间结果 %s", path)
                    except OSError:
                        log.warning("清理孤儿中间结果失败：%s", path, exc_info=True)
        except OSError:
            log.warning("扫描孤儿中间结果失败：%s", partial, exc_info=True)
        # 启动时没有 worker 在跑，partial 里的 *.tmp 都是崩溃残留的暂存文件，清掉。
        try:
            for path in partial.glob("*.tmp"):
                try:
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def public_job(self, job: dict | None) -> dict | None:
        if not job:
            return None
        job = dict(job)
        jid = job["id"]
        if job.get("thumb_path"):
            # 契约：URL 本身不含 token（token 会随任务对象进日志/截图/导出），前端拼接时再补。
            job["thumb_url"] = f"/api/jobs/{jid}/thumb"
            job["media_url"] = f"/api/jobs/{jid}/media"
        return job

    def transcript_path(self, job_id: str) -> Path:
        return self.paths.transcript_dir / f"{job_id}.json"

    def load_transcript(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        path = self.transcript_path(job_id)
        if not path.is_file():
            raise HTTPException(404, "该任务还没有转录结果")
        # Read, decide and write under one lock.  The old code read the file
        # before taking the lock and then re-checked seg_version on that stale
        # snapshot: a concurrent PUT (version 2, manual text) could be rolled
        # back to version 1 by a migration that started a moment earlier.
        with self.transcript_lock:
            try:
                transcript = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                # A damaged transcript file used to 500 on every single request
                # with no hint about the cause.  Move it aside once so later
                # reads answer the documented 404, and say what happened.
                backup = path.with_name(path.name + ".corrupt")
                try:
                    path.replace(backup)
                except OSError:
                    log.warning("字幕文件损坏且备份失败：%s", path, exc_info=True)
                log.warning("字幕文件损坏，已备份为 %s（%s）", backup, e)
                raise HTTPException(
                    500, f"字幕文件已损坏，无法读取（已备份为 {backup.name}），请重新转录"
                ) from None
            if int(transcript.get("seg_version") or 0) < SEG_VERSION:
                upgraded: list[dict] = []
                for sentence in transcript.get("sentences") or []:
                    if sentence.get("edited"):
                        upgraded.append(sentence)
                        continue
                    cleaned = cleanup.clean_sentence(sentence, self.settings)
                    if cleaned is not None:
                        upgraded.append(cleaned)
                if upgraded:
                    transcript["sentences"] = upgraded
                segment_transcript(
                    transcript,
                    max_chars=int(self.settings.get("subtitle_max_chars", 22)),
                )
                transcript["seg_version"] = SEG_VERSION
                # A migration is a write: bump the version so a client holding
                # the previous revision sees a CAS conflict instead of silently
                # overwriting the migrated text.
                transcript["version"] = int(transcript.get("version") or 1) + 1
                self.save_transcript(job_id, transcript)
            return transcript

    def save_transcript(self, job_id: str, transcript: dict) -> list[dict]:
        path = self.transcript_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A unique temp file per writer keeps concurrent saves (autosave, the
        # segment migration, and issue refreshes) from clobbering each other's
        # staging file on Windows.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{job_id}.", suffix=".json.tmp", dir=str(path.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(transcript, handle, ensure_ascii=False)
            # Windows may briefly lock the target while a reader/AV scans it.
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    break
                except (PermissionError, FileNotFoundError):
                    if attempt == 5:
                        raise
                    if not tmp.exists():
                        # An external scanner removed the staging file; write
                        # it again before the next attempt.
                        tmp.write_text(
                            json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
                        )
                    time.sleep(0.08 * (attempt + 1))
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        issues = analysis.analyze(transcript, self.settings)
        summary = analysis.summarize(issues)
        self.store.update_job(job_id, transcript_path=str(path), has_transcript=1,
                              issues_json=json.dumps(summary, ensure_ascii=False))
        pub = self.public_job(self.store.get_job(job_id))
        if pub is not None:
            self.hub.publish({"type": "job", "job": pub})
        return issues

    # ------------------------------------------------------------ jobs 生命周期

    def retry_job(self, job_id: str, fresh: bool = False) -> dict | None:
        """把任务重新排进队列。

        fresh=True（POST body {"fresh": true}）表示用户明确要求从头识别：先删
        sidecar，否则续跑会跳过已完成的段。默认保留 sidecar 续跑。
        """
        job = self.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        with self.lifecycle_lock:
            current = self.store.get_job(job_id)
            if not current:
                raise HTTPException(404, "任务不存在")
            # Retrying a running job used to return "queued" while the old run
            # kept going and wrote done at the end (nothing was re-run).
            if current["status"] == "processing":
                raise HTTPException(409, "任务正在处理，请先取消后再重试")
            if fresh:
                existed = self.pipeline.partial_path(job_id).is_file()
                if not self.pipeline.discard_partial(job_id) and existed:
                    raise HTTPException(500, "无法清理中间结果，任务未重新排队")
                # 从头重跑也清掉降级记忆：否则 OOM 降过级的任务即使 fresh 也永久
                # batch=1（约 2.2× 慢），直到引擎重启才恢复。
                self.pipeline.batch_overrides.pop(job_id, None)
                log.info("job %s 按 fresh 重跑：已丢弃中间结果", job_id)
            self.deleted.discard(job_id)
            self.cancel_requested.discard(job_id)
            # 保留 has_transcript/issues_json：旧稿（可能含用户编辑）在新稿落盘前
            # 仍可访问。重试再失败时不会把一份仍在盘上的字幕变成 UI 不可达（P1-1）。
            self.store.update_job(job_id, status="queued", stage="queued", progress=0.0,
                                  error=None, started_at=None, finished_at=None,
                                  rtf=None, elapsed_s=None)
            row = self.public_job(self.store.get_job(job_id))
        self.hub.publish({"type": "job", "job": row})
        return row

    def remove_job(self, job_id: str) -> None:
        """删任务：先标记/取消，等 worker 退出，再删掉它名下所有文件。"""
        job = self.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        with self.lifecycle_lock:
            self.deleted.add(job_id)
            self.cancel_requested.add(job_id)
            self.worker.cancel(job_id)
        # Deleting while ASR was running used to recreate partial.json after
        # the delete (orphan file, no DB row).  Wait for the worker to leave
        # this job before touching its files.
        deadline = time.time() + 30.0
        while time.time() < deadline and self.worker.current == job_id:
            time.sleep(0.05)
        if self.worker.current == job_id:
            log.warning("删除任务 %s 时 worker 仍未退出，继续清理文件", job_id)
        # Hold the transcript lock while removing files: an edit that already
        # loaded this transcript would otherwise save it back right after the
        # unlink and leave an orphan <id>.json with no DB row behind.
        with self.transcript_lock:
            for p in (self.transcript_path(job_id),
                      self.paths.thumb_dir / f"{job_id}.jpg",
                      self.paths.cache_dir / f"{job_id}.wav",
                      # 音轨指纹 / ffmpeg stderr 侧文件与 wav 同名同目录，
                      # 漏删就是 data/cache 里的永久垃圾
                      self.paths.cache_dir / f"{job_id}.wav.meta.json",
                      self.paths.cache_dir / f"{job_id}.wav.ffmpeg.log"):
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    log.warning("删除任务文件失败：%s", p, exc_info=True)
        # sidecar 与降级记忆必须一起清：留着就是垃圾，而且同名 id 重跑会读到旧结果
        self.pipeline.discard_partial(job_id)
        self.pipeline.batch_overrides.pop(job_id, None)
        with self.lifecycle_lock:
            self.store.delete_job(job_id)
            # Only clear the tombstone once the worker really left this job;
            # otherwise a slow final commit could still write an orphan
            # transcript for a row that no longer exists.
            if self.worker.current != job_id:
                self.deleted.discard(job_id)
                self.cancel_requested.discard(job_id)
        self.hub.publish({"type": "queue", "queue": self.worker.queue_info()})


# ---------------------------------------------------------------- worker


class Worker(threading.Thread):
    def __init__(self, state: State):
        super().__init__(name="voicetrace-worker", daemon=True)
        self.state = state
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.cancels: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.current: str | None = None

    def _schedule_oom_retry(self, job: dict, exc: BaseException | None) -> bool:
        """B2：显存不足时把任务降级到 batch=1 重新入队，返回是否已入队。

        只在「本进程内还没自动重试过」时做一次；已经降过仍失败就交给调用方
        报最终错误。用户手动取消的任务不在此列（canceled 不是失败）。
        """
        s = self.state
        jid = job["id"]
        if not is_out_of_memory(exc):
            return False
        with s.lifecycle_lock:
            row = s.store.get_job(jid)
            if not row or row.get("status") == "canceled":
                return False
            # A user cancel that landed while the OOM path was unwinding must
            # win: the job used to be requeued and re-run against the user's
            # explicit request.
            with self.lock:
                ev = self.cancels.get(jid)
            if jid in s.cancel_requested or jid in s.deleted or (ev and ev.is_set()):
                s.store.update_job(jid, status="canceled", stage="canceled",
                                   finished_at=now_iso(), error=None)
                pub = s.public_job(s.store.get_job(jid))
                if pub is not None:
                    s.hub.publish({"type": "job", "job": pub})
                return False
            # 降级记忆就是 batch_overrides 本身：已经降到 1 就不再自动重试。
            # 成功 / 删除 / fresh 重跑都会 pop 它，预算随本次运行自然复位。
            if jid in s.pipeline.batch_overrides:
                return False
            # 下一次用 batch=1 跑；已完成的段靠 sidecar 续跑，不会重算
            s.pipeline.batch_overrides[jid] = 1
            # 重新入队必须和上面的 cancel 检查同在一把 lifecycle_lock 内：
            # 否则 error→queued 窗口里落地的 /cancel 只写进 cancel_requested，
            # 而 worker 认领时会 discard(jid) 把它抹掉，任务照旧重跑一遍。
            s.store.update_job(jid, status="queued", stage="queued", progress=0.0,
                               error=None, started_at=None, finished_at=None)
        log.warning("job %s 显存不足，降级到 batch=1 自动重试", jid)
        s.hub.publish({"type": "toast", "level": "warn",
                       "message": OOM_DOWNGRADE_MESSAGE})
        pub = s.public_job(s.store.get_job(jid))
        if pub is not None:
            s.hub.publish({"type": "job", "job": pub})
        s.hub.publish({"type": "queue", "queue": self.queue_info()})
        return True

    def _auto_export_srt(self, job: dict, transcript: dict) -> None:
        """auto_export 开启时，转录完成后把 SRT 写到视频所在目录。

        与手动导出共用同一套落盘规则：临时文件 + os.replace、同名追加
        (2)/(3) 绝不静默覆盖、拒绝 Windows 保留设备名；视频目录不可写
        （只读盘、权限不足）时回退 data/exports 并明确提示。导出失败只记
        日志与提示，不能把已经完成的任务改成失败。
        """
        s = self.state
        if not s.settings.get("auto_export"):
            return
        src = Path(str(job.get("path") or ""))
        stem = src.stem or str(job.get("id") or "subtitle")
        name = f"{stem}.srt"
        if is_reserved_device_name(name):
            name = "_" + name
        fallback = s.paths.data_dir / "exports"
        targets: list[Path] = []
        if src.is_absolute() and src.parent.is_dir():
            try:
                targets.append(unique_export_path(src.parent, name))
            except OSError as e:
                log.warning("auto export: video dir unusable (%s)", e)
        try:
            targets.append(unique_export_path(fallback, name))
        except OSError as e:
            log.warning("auto export: fallback dir unusable (%s)", e)
            return
        try:
            content = exporter.export(transcript, "srt", s.settings)
        except Exception as e:  # noqa: BLE001 - 导出失败不能影响任务状态
            log.warning("auto export: cannot render srt for %s (%s)",
                        job.get("id"), e)
            return
        seen: set[Path] = set()
        for out in targets:
            if out in seen:
                continue
            seen.add(out)
            try:
                atomic_write_text(out, content, "utf-8-sig")
            except OSError as e:
                log.warning("auto export failed: %s (%s)", out, e)
                continue
            if out.parent == fallback:
                s.hub.publish({
                    "type": "toast", "level": "warn",
                    "message": f"视频目录不可写，字幕已导出到：{out}",
                })
            else:
                s.hub.publish({
                    "type": "toast", "level": "info",
                    "message": f"字幕已自动导出：{out}",
                })
            return
        s.hub.publish({
            "type": "toast", "level": "warn",
            "message": f"{stem}.srt 自动导出失败：没有可写的目录",
        })

    def cancel(self, job_id: str) -> None:
        with self.lock:
            ev = self.cancels.get(job_id)
        if ev:
            ev.set()

    def run(self) -> None:
        """Worker main loop.

        The queue scaffolding (next_queued / claim / queue broadcast) used to
        sit outside every try/except: one transient sqlite error ("database is
        locked", disk full) killed the only worker thread, after which every
        queued job silently stayed queued until the engine was restarted.  A
        failed iteration is now logged and retried after a short back-off.
        """
        log.info("worker started")
        while not self.stop.is_set():
            if self.paused.is_set():
                self.stop.wait(0.4)
                continue
            try:
                self._run_once()
            except Exception:  # noqa: BLE001 - the loop must survive a bad tick
                log.exception("worker: 本轮调度异常，1 秒后重试")
                self.stop.wait(1.0)
        log.info("worker stopped")

    def _run_once(self) -> None:
        """One scheduling iteration: claim at most one queued job and run it."""
        s = self.state
        job = s.store.next_queued()
        if not job:
            self.stop.wait(0.4)
            return
        jid = job["id"]
        ev = threading.Event()
        # Claim atomically: re-read the row under the lifecycle lock so a
        # cancel/delete that landed after next_queued() is not overwritten
        # by an unconditional status=processing write.
        with s.lifecycle_lock:
            current = s.store.get_job(jid)
            if (not current or current.get("status") != "queued"
                    or jid in s.deleted):
                self.stop.wait(0.05)
                return
            s.cancel_requested.discard(jid)
            s.store.update_job(jid, status="processing",
                               stage="load" if not s.runtime.ready else "probe",
                               progress=0.0, started_at=now_iso(),
                               finished_at=None, error=None)
            with self.lock:
                self.cancels[jid] = ev
                self.current = jid
        s.hub.publish({"type": "queue", "queue": self.queue_info()})
        try:
            transcript = s.pipeline.run(jid, ev)
            if transcript:
                # 只有真正转录成功（非取消、非异常）才自动导出。
                self._auto_export_srt(job, transcript)
        except Exception as e:  # noqa: BLE001
            # A failed first DB write leaves the row queued; without a
            # short back-off the worker would pick the same job again in a
            # tight loop and leak one cancel event per iteration.
            try:
                row = s.store.get_job(jid)
            except Exception:  # noqa: BLE001 - DB still broken
                row = None
            if row is None or row.get("status") == "queued":
                self.stop.wait(1.0)
            if is_out_of_memory(e):
                # 显存不足不是「任务内容坏了」：先降级重试一次，还不行才报错
                if self._schedule_oom_retry(job, e):
                    log.warning("job %s 已降级重新入队", jid)
                else:
                    log.warning("job %s 显存不足且已自动降级过，不再重试", jid)
                    s.hub.publish({"type": "toast", "level": "error",
                                   "message": OOM_MESSAGE})
            else:
                log.exception("job failed: %s", jid)
                s.hub.publish({"type": "toast", "level": "error",
                               "message": f"{job.get('name') or jid} 识别失败：{e}"})
        finally:
            with self.lock:
                self.cancels.pop(jid, None)
                self.current = None
            with s.lifecycle_lock:
                s.cancel_requested.discard(jid)
            s.hub.publish({"type": "queue", "queue": self.queue_info()})

    def queue_info(self) -> dict:
        s = self.state
        return {
            "paused": self.paused.is_set(),
            "pending": s.store.pending_count(),
            "current": self.current,
        }


# ---------------------------------------------------------------- models


class ImportBody(BaseModel):
    paths: list[str] = Field(default_factory=list)
    # 默认全部自动校正：前端不再传参时也按开启处理（显式 false 仍然生效）。
    auto_correct: bool = True


class AutoCorrectBody(BaseModel):
    enabled: bool = True


class SettingsBody(BaseModel):
    """Settings payload.

    The documented shape is ``{"settings": {...}}`` but the desktop client
    historically PUT a flat Settings object. Both are accepted here so a saved
    setting never silently disappears because of a body-shape mismatch.
    """

    model_config = ConfigDict(extra="allow")

    settings: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        if isinstance(self.settings, dict) and self.settings:
            return dict(self.settings)
        extra = getattr(self, "model_extra", None) or {}
        return {k: v for k, v in extra.items() if k != "settings"}


class TextBody(BaseModel):
    text: str = ""
    # Optional optimistic concurrency: reject the write when the client edited
    # an older revision of the transcript.
    expected_version: int | None = None


class ActionBody(BaseModel):
    actions: list[dict[str, Any]] = Field(default_factory=list)
    expected_version: int | None = None


class ExportBody(BaseModel):
    format: str = "srt"
    path: str | None = None


class QueueBody(BaseModel):
    paused: bool = False


class RetryBody(BaseModel):
    """retry 的请求体；缺省 {}

    fresh=True 表示用户要「从头再跑」：先删 sidecar，续跑状态一并作废。
    """

    fresh: bool = False


class ModelDownloadBody(BaseModel):
    """POST /api/models/download 的请求体。

    repos 缺省时下载全部缺失模型；proxy / endpoint 只接受 http(s) URL，
    避免把任意字符串塞进子进程环境变量。
    """

    repos: list[str] | None = None
    proxy: str | None = None
    endpoint: str | None = None


class RuntimeInstallBody(BaseModel):
    """POST /api/runtime/install 的请求体（CUDA PyTorch 运行时）。"""

    proxy: str | None = None
    mirror: bool = False


# ---------------------------------------------------------------- app


def build_app(state: State) -> FastAPI:
    app = FastAPI(title="VoiceTrace Engine", version=VERSION, docs_url=None,
                  redoc_url=None, openapi_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    )

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path in ("/api/health", "/health"):
            return await call_next(request)
        token = ""
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        elif query_token_allowed(path):
            token = request.query_params.get("token") or ""
        if not same_token(token, state.token):
            return JSONResponse(
                {"error": {"code": "unauthorized", "message": "无效的访问令牌"}},
                status_code=401,
            )
        return await call_next(request)

    def error_envelope(code: str, message: str, status_code: int,
                       headers: dict | None = None) -> JSONResponse:
        return JSONResponse({"error": {"code": code, "message": message}},
                            status_code=status_code, headers=headers)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_envelope(f"http_{exc.status_code}", str(exc.detail),
                              exc.status_code, getattr(exc, "headers", None))

    @app.exception_handler(StarletteHTTPException)
    async def routing_error(request: Request, exc: StarletteHTTPException):
        # Raised by the router itself: unknown path (404), wrong method (405),
        # and anything else Starlette rejects before a handler runs.
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(
            exc.detail, ensure_ascii=False)
        return error_envelope(f"http_{exc.status_code}", detail,
                              exc.status_code, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = exc.errors() or [{}]
        first = errors[0]
        loc = ".".join(str(part) for part in (first.get("loc") or ()))
        message = str(first.get("msg") or "请求参数不合法")
        if loc:
            message = f"{loc}: {message}"
        return error_envelope("validation_error", message, 422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # noqa: ANN001
        # Only the path is logged: the query string may carry the bearer token.
        log.exception("unhandled error on %s", request.url.path)
        return error_envelope("internal", "引擎内部错误，详情见 data/logs/engine.log", 500)

    # ------------------------------------------------------------- basics

    def system_payload(include_gpu: bool = True) -> dict:
        """Shared by GET /api/system and the websocket snapshot.

        The WS snapshot runs on the event loop, so it reuses the cached GPU
        probe instead of paying the first (multi-second) torch import.
        """
        models_present = {}
        for key in ("aed", "vad", "punc"):
            models_present[key] = is_model_ready(state.paths.models_dir, key)
        if include_gpu:
            gpu_info = gpu_snapshot()
        else:
            gpu_info = dict(_GPU_INFO.get("info") or {})
        device = state.runtime.device if state.runtime.ready else (
            "cuda" if (gpu_info.get("cuda") and state.runtime.use_gpu) else "cpu"
        )
        payload = {
            "version": VERSION,
            "ready": state.runtime.ready,
            "loading": state.runtime.loading,
            "model_state": model_state(state.runtime),
            "models_present": models_present,
            "models_download": state.models.status(),
            "runtime_setup": state.runtime_setup.status(),
            "error": state.runtime.error,
            "device": device,
            "sample_rate": SAMPLE_RATE,
            "queue": state.worker.queue_info(),
            "paths": {"data": str(state.paths.data_dir),
                      "models": str(state.paths.models_dir)},
            "ffmpeg": str(state.ffmpeg),
            "uptime_s": round(time.time() - state.started, 1),
        }
        if gpu_info:
            payload["gpu"] = gpu_info
        return payload

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "uptime_s": round(time.time() - state.started, 1)}

    @app.get("/api/system")
    def system():
        return system_payload()

    # --------------------------------------------------------------- models

    def clean_url(value: str | None) -> str:
        text = (value or "").strip()
        if text and not text.lower().startswith(("http://", "https://")):
            raise HTTPException(400, "代理/镜像地址必须以 http:// 或 https:// 开头")
        return text

    @app.get("/api/models")
    def models_status():
        return state.models.status()

    @app.post("/api/models/download")
    def models_download(body: ModelDownloadBody | None = None):
        body = body or ModelDownloadBody()
        return state.models.start(
            repos=body.repos,
            proxy=clean_url(body.proxy),
            endpoint=clean_url(body.endpoint),
        )

    @app.post("/api/models/cancel")
    def models_cancel():
        return state.models.cancel()

    @app.get("/api/runtime")
    def runtime_status():
        return state.runtime_setup.status()

    @app.post("/api/runtime/install")
    def runtime_install(body: RuntimeInstallBody | None = None):
        body = body or RuntimeInstallBody()
        return state.runtime_setup.start(
            proxy=clean_url(body.proxy),
            mirror=body.mirror,
        )

    @app.post("/api/runtime/cancel")
    def runtime_cancel():
        return state.runtime_setup.cancel()

    @app.get("/api/settings")
    def get_settings():
        return state.settings

    @app.put("/api/settings")
    def put_settings(body: SettingsBody):
        patch = {k: v for k, v in body.payload().items() if k in state.settings}
        with state.settings_lock:
            merged = dict(state.settings)
            merged.update(patch)
            # Persist first and publish only on success: the old code cleared
            # the live dict before writing, so a failed write left the engine
            # with no settings and every later patch was filtered away.
            saved = save_settings(state.paths.settings_path, merged)
            changed = {k for k, v in saved.items() if state.settings.get(k) != v}
            # Update in place, never clear(): the same dict object is read by
            # the worker / exporter / segmentation while a job runs, and a
            # clear()+update() pair leaves a window where every key is missing
            # (settings.get("auto_export") -> None silently skipped auto
            # export, _asr_signature() -> None discarded a resume sidecar).
            state.settings.update(saved)
            state.runtime.settings = state.settings
            # half/beam/device are baked into the loaded models: be explicit
            # that they only take effect on the next engine start instead of
            # pretending the running models reloaded.
            deferred = changed & {"device", "half", "beam_size"}
            if deferred and state.runtime.ready:
                state.hub.publish({
                    "type": "toast", "level": "info",
                    "message": "设备/半精度/beam 设置将在下次启动引擎后生效",
                })
            return dict(state.settings)

    # --------------------------------------------------------------- jobs

    @app.post("/api/jobs")
    def create_jobs(body: ImportBody):
        added, skipped = [], []
        existing = {j["path"]: j for j in state.store.list_jobs()
                    if j["status"] in ("queued", "processing")}
        for raw in body.paths:
            p = Path(raw).expanduser()
            try:
                p = p.resolve()
            except OSError:
                pass
            if not p.is_file():
                skipped.append({"path": str(p), "reason": "文件不存在"})
                continue
            if p.suffix.lower() not in MEDIA_EXTS:
                skipped.append({"path": str(p), "reason": "不支持的格式"})
                continue
            if str(p) in existing:
                skipped.append({"path": str(p), "reason": "已在队列中"})
                continue
            info = None
            try:
                info = probe(state.ffprobe, p)
            except Exception as e:  # noqa: BLE001
                log.warning("probe failed for %s: %s", p, e)
            # A file that vanished (or became unreadable) while the probe ran
            # used to raise out of the handler and 500 the whole import batch.
            try:
                size_bytes = p.stat().st_size
            except OSError:
                skipped.append({"path": str(p), "reason": "文件不可读"})
                continue
            job = state.store.add_job(
                str(p), p.name, size_bytes,
                info.duration_ms if info else 0,
                info.width if info else 0,
                info.height if info else 0,
                auto_correct=body.auto_correct,
            )
            # Keep the in-batch dedupe set current: two identical paths in one
            # request used to create two jobs because only the pre-request
            # snapshot was consulted.
            existing[str(p)] = job
            added.append(state.public_job(job))
        state.hub.publish({"type": "queue", "queue": state.worker.queue_info()})
        return {"jobs": added, "skipped": skipped}

    @app.get("/api/jobs")
    def list_jobs():
        jobs = [state.public_job(j) for j in state.store.list_jobs()]
        return {"jobs": jobs, "queue": state.worker.queue_info()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = state.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return state.public_job(job)

    @app.post("/api/jobs/{job_id}/auto-correct")
    def set_auto_correct(job_id: str, body: AutoCorrectBody):
        job = state.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        state.store.update_job(job_id, auto_correct=1 if body.enabled else 0)
        return state.public_job(state.store.get_job(job_id))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        # 连带删掉 <data_dir>/partial/<id>.json，别给用户留垃圾
        state.remove_job(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, body: RetryBody | None = None):
        # body 缺省 = 保留 sidecar 续跑；{"fresh": true} = 丢掉中间结果从头跑
        return state.retry_job(job_id, fresh=bool(body and body.fresh))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        # Serialise with the worker claim and the pipeline's final commit: a
        # cancel that lands between "dequeued" and "status=processing" used to
        # be overwritten, and a cancel just before the final save still ended
        # as done.
        with state.lifecycle_lock:
            job = state.store.get_job(job_id)
            if not job:
                raise HTTPException(404, "任务不存在")
            state.cancel_requested.add(job_id)
            if job["status"] == "queued":
                state.store.update_job(job_id, status="canceled", stage="done",
                                       finished_at=now_iso())
            elif job["status"] == "processing":
                state.worker.cancel(job_id)
            row = state.public_job(state.store.get_job(job_id))
        state.hub.publish({"type": "job", "job": row})
        return row

    @app.post("/api/queue")
    def set_queue(body: QueueBody):
        if body.paused:
            state.worker.paused.set()
        else:
            state.worker.paused.clear()
        state.hub.publish({"type": "queue", "queue": state.worker.queue_info()})
        return state.worker.queue_info()

    @app.get("/api/queue")
    def get_queue():
        return state.worker.queue_info()

    # --------------------------------------------------------- transcript

    @app.get("/api/jobs/{job_id}/transcript")
    def get_transcript(job_id: str):
        return state.load_transcript(job_id)

    @app.get("/api/jobs/{job_id}/history")
    def get_history(job_id: str):
        # Undo availability is a server fact: the frontend used to reset its
        # depth to 0 on every load, disabling undo while history still existed.
        if not state.store.get_job(job_id):
            raise HTTPException(404, "任务不存在")
        return {"count": state.store.history_count(job_id)}

    @app.get("/api/jobs/{job_id}/issues")
    def get_issues(job_id: str):
        transcript = state.load_transcript(job_id)
        issues = analysis.analyze(transcript, state.settings)
        return {"issues": issues, "summary": analysis.summarize(issues)}

    @app.put("/api/jobs/{job_id}/sentences/{sid}")
    def put_sentence(job_id: str, sid: int, body: TextBody):
        # Read-modify-write under one reentrant state lock: 8 concurrent edits
        # used to load the same snapshot and clobber each other's sentences.
        with state.transcript_lock:
            transcript = state.load_transcript(job_id)
            check_version(transcript, body.expected_version)
            sentence = next((s for s in transcript.get("sentences") or []
                             if s["id"] == sid), None)
            if sentence is None:
                raise HTTPException(404, "句子不存在")
            state.store.push_history(job_id,
                                     json.dumps(transcript, ensure_ascii=False), "edit")
            edit.set_sentence_text(sentence, body.text)
            transcript["version"] = int(transcript.get("version") or 1) + 1
            _restat(transcript)
            issues = state.save_transcript(job_id, transcript)
            return {"transcript": transcript, "issues": issues}

    @app.post("/api/jobs/{job_id}/apply")
    def apply_actions(job_id: str, body: ActionBody):
        with state.transcript_lock:
            transcript = state.load_transcript(job_id)
            if not body.actions:
                issues = analysis.analyze(transcript, state.settings)
                return {"transcript": transcript, "issues": issues}
            check_version(transcript, body.expected_version)
            state.store.push_history(job_id,
                                     json.dumps(transcript, ensure_ascii=False), "apply")
            edit.apply_actions(transcript, body.actions, state.settings)
            _restat(transcript)
            issues = state.save_transcript(job_id, transcript)
            return {"transcript": transcript, "issues": issues}

    @app.post("/api/jobs/{job_id}/undo")
    def undo(job_id: str, expected_version: int | None = None):
        with state.transcript_lock:
            current = state.load_transcript(job_id)
            check_version(current, expected_version)
            # Peek first, consume only after the new transcript is on disk:
            # popping before the write lost the snapshot when the write failed
            # (first undo 500, retry 409, history empty).
            row = state.store.peek_history(job_id)
            if not row:
                raise HTTPException(409, "没有可撤销的操作")
            transcript = json.loads(row["transcript_json"])
            # keep versions monotonic so clients never mistake an undo for the
            # state they already rendered
            transcript["version"] = int(current.get("version") or 1) + 1
            issues = state.save_transcript(job_id, transcript)
            state.store.delete_history(row["id"])
            return {"transcript": transcript, "issues": issues, "undone": True}

    @app.post("/api/jobs/{job_id}/export")
    def export_transcript(job_id: str, body: ExportBody):
        job = state.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        transcript = state.load_transcript(job_id)
        fmt = (body.format or "srt").lower().lstrip(".")
        ext = EXPORT_EXTS.get(fmt)
        if ext is None:
            raise HTTPException(400, f"不支持的格式 {fmt}")
        stem = Path(job["path"]).stem or job["id"]
        if body.path:
            # Explicit paths are user-chosen, so they must be unambiguous:
            # absolute, with the extension the format actually produces.
            requested = Path(body.path).expanduser()
            if not requested.is_absolute():
                raise HTTPException(400, "导出路径必须是绝对路径")
            if requested.is_dir():
                requested = requested / f"{stem}{ext}"
            if requested.suffix.lower() != ext:
                raise HTTPException(
                    400, f"导出扩展名须为 {ext}，收到 {requested.suffix or '无扩展名'}")
            if is_reserved_device_name(requested.name):
                raise HTTPException(
                    400, "不允许写入 Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）")
        else:
            configured = str(state.settings.get("export_dir") or "").strip()
            base_dir = (Path(configured).expanduser() if configured
                        else state.paths.data_dir / "exports")
            if not base_dir.is_absolute():
                # A relative export_dir used to be written relative to the
                # engine CWD (the portable folder's root) and echoed back as a
                # relative path, so the desktop client could not open it.
                base_dir = base_dir.resolve()
            name = f"{stem}{ext}"
            if is_reserved_device_name(name):
                name = "_" + name
            requested = unique_export_path(base_dir, name)
        try:
            content = exporter.export(transcript, fmt, state.settings)
        except ValueError as e:
            raise HTTPException(400, str(e))

        enc = "utf-8-sig" if fmt in ("srt", "ass") else "utf-8"
        # The fallback is a *default* export target, so it obeys the same
        # "never silently overwrite" rule as the default branch above: a failed
        # write to the requested path used to overwrite whatever earlier export
        # happened to sit at data/exports/<same name>.srt.
        fallback = unique_export_path(state.paths.data_dir / "exports", requested.name)
        candidates = [requested, fallback]
        last_error: OSError | None = None
        for index, out in enumerate(candidates):
            try:
                # temp file + os.replace: a crash never leaves a truncated srt
                atomic_write_text(out, content, enc)
                return {
                    "path": str(out),
                    "fallback": index > 0,
                    "requested_path": str(requested),
                }
            except (OSError, UnicodeError) as e:
                last_error = e
                log.warning("export candidate failed: %s (%s)", out, e)
        raise HTTPException(
            400,
            f"无法写入 {requested}（已尝试默认导出目录）：{last_error}",
        )

    # -------------------------------------------------------------- media

    @app.get("/api/jobs/{job_id}/media")
    def media(job_id: str, request: Request):
        job = state.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        path = Path(job["path"])
        # is_file() and stat() are not one call: a video deleted between them
        # used to raise out of the handler (500) instead of the documented 404.
        try:
            size = path.stat().st_size
        except OSError:
            raise HTTPException(404, "视频文件已被移动或删除") from None
        if not path.is_file():
            raise HTTPException(404, "视频文件已被移动或删除")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = request.headers.get("range")
        if not rng:
            return StreamingResponse(
                _iter_file(path, 0, size - 1), media_type=ctype,
                headers={"Accept-Ranges": "bytes", "Content-Length": str(size)},
            )
        try:
            unit, _, spec = rng.partition("=")
            if unit.strip().lower() != "bytes":
                raise ValueError
            start_s, _, end_s = spec.partition("-")
            if not start_s:
                # "bytes=-N" is a suffix range: the LAST N bytes.  It used to
                # be parsed as "0-N" and returned the first N bytes.
                suffix = int(end_s)
                if suffix <= 0:
                    raise ValueError
                start = max(0, size - suffix)
                end = size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                raise ValueError
        except ValueError:
            return Response(status_code=416,
                            headers={"Content-Range": f"bytes */{size}"})
        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        }
        if request.method == "HEAD":
            return Response(status_code=206, headers=headers, media_type=ctype)
        return StreamingResponse(_iter_file(path, start, end), status_code=206,
                                 media_type=ctype, headers=headers)

    @app.get("/api/jobs/{job_id}/thumb")
    def thumb(job_id: str):
        # Validate the id against the store *before* touching the file system:
        # an unchecked id could contain ..\\ and read a JPEG outside data\\thumbs.
        job = state.store.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        path = thumb_file(state.paths.thumb_dir, job_id)
        if not path.is_file():
            raise HTTPException(404, "暂无封面")
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=3600"})

    # ----------------------------------------------------------------- ws

    async def ws_endpoint(ws: WebSocket):
        token = ws.query_params.get("token") or ""
        if not same_token(token, state.token):
            await ws.close(code=4401)
            return
        state.hub.loop = asyncio.get_running_loop()

        def snapshot_payload() -> dict:
            # Built *after* the client joined the fan-out (see Hub.connect), so
            # it includes every change published while the socket was accepted.
            return {
                "type": "snapshot",
                "jobs": [state.public_job(j) for j in state.store.list_jobs()],
                "queue": state.worker.queue_info(),
                "engine": system_payload(include_gpu=False),
            }

        try:
            await state.hub.connect(ws, snapshot_payload)
        except Exception:  # noqa: BLE001 - the client vanished during accept
            state.hub.disconnect(ws)
            return
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            state.hub.disconnect(ws)

    app.add_api_websocket_route("/ws", ws_endpoint)
    app.add_api_websocket_route("/api/ws", ws_endpoint)

    return app


def _iter_file(path: Path, start: int, end: int) -> Iterable[bytes]:
    with open(path, "rb") as f:
        f.seek(start)
        left = end - start + 1
        while left > 0:
            chunk = f.read(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk


def _restat(transcript: dict) -> None:
    sentences = transcript.get("sentences") or []
    stats = transcript.setdefault("stats", {})
    # SPEC §3.2: stats.chars counts content characters (no punctuation), the
    # same unit the editor, the segmenter and the exporter report.
    stats["chars"] = sum(count_chars(s.get("text") or "") for s in sentences)
    stats["sentences"] = len(sentences)


# ---------------------------------------------------------------- entry


def _request_shutdown(server: uvicorn.Server | None) -> None:
    """Ask uvicorn to unwind normally instead of killing the process."""
    if server is None:
        return
    try:
        server.should_exit = True
    except Exception:  # noqa: BLE001 - shutdown must never crash
        log.exception("failed to request a graceful shutdown")


def _watch_parent(pid: int, server: uvicorn.Server | None = None) -> None:
    """Shut down when the desktop shell dies so no orphan server keeps running.

    os._exit(0) used to skip every cleanup step (port file stayed behind, the
    WAL never checkpointed); setting should_exit lets the main thread run the
    normal shutdown path instead.
    """
    import ctypes

    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        # No handle means no watchdog at all: say so instead of failing quiet.
        log.warning("parent watchdog unavailable for pid %s", pid)
        return
    try:
        while True:
            rc = kernel32.WaitForSingleObject(handle, 2000)
            if rc == 0:  # parent exited
                log.info("parent process %s exited, shutting down", pid)
                _request_shutdown(server)
                return
    finally:
        kernel32.CloseHandle(handle)


def _setup_logging(paths: Paths) -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(RotatingFileHandler(
            paths.log_dir / "engine.log", maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
        ))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # jieba logs dictionary loading at DEBUG; keep engine.log readable.
    try:
        import jieba

        if hasattr(jieba, "setLogLevel"):
            jieba.setLogLevel(logging.INFO)
        logging.getLogger("jieba").setLevel(logging.INFO)
    except Exception:  # noqa: BLE001 - optional dependency
        pass


def write_port_file(path: Path | None, port: int) -> None:
    """Publish {port, version, pid} for tooling; the token is never written."""
    if path is None:
        return
    payload = {"port": port, "version": VERSION, "pid": os.getpid()}
    try:
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False), "utf-8")
    except OSError:
        log.warning("could not write port file %s", path)


def remove_port_file(path: Path | None) -> None:
    """Drop the handshake file on every exit path (normal, SIGINT, orphan)."""
    if path is None:
        return
    for _ in range(5):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05)
    log.warning("could not remove port file %s", path)


def shutdown(state: State, port_file: Path | None = None) -> None:
    """Graceful teardown: stop the worker/ffmpeg, checkpoint, drop the file."""
    try:
        state.worker.stop.set()
        # Cancelling through the pipeline also kills the ffmpeg child that
        # owns the current wav (media.py watches this event).
        state.pipeline.cancel_all()
        if state.worker.is_alive():
            state.worker.join(timeout=5.0)
        if state.worker.is_alive():
            # A model load / inference can outlive the 5 s budget.  Closing the
            # store underneath it produced "Cannot operate on a closed
            # database"; wait longer, and if it still will not stop, leave the
            # store open (process exit reclaims it) instead of racing it.
            log.warning("worker 5 秒内未退出，继续等待最多 25 秒")
            state.worker.join(timeout=25.0)
    except Exception:  # noqa: BLE001
        log.warning("stopping the worker failed", exc_info=True)
    if state.worker.is_alive():
        log.error("worker 仍未退出，跳过 checkpoint/close 以免访问已关闭的数据库")
        remove_port_file(port_file)
        return
    try:
        state.store.checkpoint()
    except Exception:  # noqa: BLE001
        log.warning("wal checkpoint failed", exc_info=True)
    try:
        state.store.close()
    except Exception:  # noqa: BLE001
        log.warning("closing the store failed", exc_info=True)
    remove_port_file(port_file)
    log.info("engine stopped")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="voicetrace-engine")
    p.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    p.add_argument("--token", default="")
    p.add_argument("--data-dir", default="")
    p.add_argument("--models-dir", default="")
    p.add_argument("--ffmpeg", default="", help="directory containing ffmpeg/ffprobe")
    p.add_argument("--engine-root", default=str(Path(__file__).resolve().parent.parent))
    p.add_argument("--parent-pid", type=int, default=0)
    p.add_argument("--gpu", action="store_true", default=True)
    p.add_argument("--no-gpu", dest="gpu", action="store_false")
    p.add_argument("--port-file", default="")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state = State(args)
    _setup_logging(state.paths)
    log.info("VoiceTrace engine %s starting, data=%s", VERSION, state.paths.data_dir)

    app = build_app(state)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(128)
    port = sock.getsockname()[1]

    config = uvicorn.Config(app, log_config=None, access_log=False, log_level="warning")
    server = uvicorn.Server(config)
    # 壳不再传 --port-file：默认写在 <data>/port.json，壳从这里读端口（不含 token）。
    port_file = (
        Path(args.port_file).expanduser()
        if args.port_file
        else state.paths.data_dir / "port.json"
    )

    if args.parent_pid:
        threading.Thread(target=_watch_parent, args=(args.parent_pid, server),
                         daemon=True).start()

    state.worker.start()
    # stdout is appended to engine.log by the shell: never print the token.
    print(json.dumps({"type": "ready", "port": port, "version": VERSION,
                      "pid": os.getpid()}), flush=True)
    write_port_file(port_file, port)
    log.info("listening on 127.0.0.1:%s", port)

    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        shutdown(state, port_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
