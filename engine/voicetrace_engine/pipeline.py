"""Long-video transcription pipeline.

One job = one video.  The pipeline is deliberately sequential and
memory-bounded so a two-hour lecture is just as safe as a ten-second clip:

    probe -> thumb -> wav -> VAD windows -> ASR batches -> punctuation
          -> transcript -> issue analysis

Every stage reports progress and observes the cancel event between batches.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from . import analysis, cleanup, edit
from . import runtime as runtime_module
from .config import VERSION, Paths
from .media import extract_audio, make_thumb, probe
from .runtime import AsrItem, AsrResult, Cancelled, Runtime, Token
from .segmentation import SEG_VERSION, segment_transcript
from .store import Store, now_iso
from .textutil import count_chars

log = logging.getLogger("voicetrace.pipeline")

EmitFn = Callable[[dict], None]

# stage weights: the first job pays ~10% for the model load, the rest is
# fixed overhead plus extract / vad / asr / punc.
P_LOAD = (0.00, 0.10)
P_PROBE = 0.12
P_EXTRACT = (0.12, 0.20)
P_VAD = (0.20, 0.30)
P_ASR = (0.30, 0.925)
P_PUNC = (0.925, 0.985)

WINDOW_CTX_MS = 8000
MAX_SEGMENT_MS = 28000
MIN_SEGMENT_MS = 160

# 中间结果 sidecar：<data_dir>/partial/<job_id>.json（配置里没有这个目录，
# 由本模块按需创建，避免为了它去改 config.Paths）。
# v2: per-result segment boundaries + media/settings fingerprint.
PARTIAL_VERSION = 2
PARTIAL_DIRNAME = "partial"
# batch_overrides 里没有这个任务时的「很大值」，min() 后会落回设置值。
NO_BATCH_OVERRIDE = 1 << 30
# 降级重试退化成 batch=1 仍 OOM 时给用户的可执行中文说明
OOM_MESSAGE = (
    "显存不足：已自动降到 batch=1 仍失败。请关闭占用显卡的程序后重试；"
    "已完成的段落已保存，重试会续跑。"
)
OOM_DOWNGRADE_MESSAGE = "显存不足，已自动降级到 batch=1 重试（已完成的段会续跑）"


def _lerp(rng: tuple[float, float], t: float) -> float:
    a, b = rng
    return a + (b - a) * max(0.0, min(1.0, t))


class JobError(RuntimeError):
    """Business failure: a stable machine code plus a user-facing message.

    The pipeline used to surface raw English exceptions (ffmpeg stderr, torch
    errors) in the job row; the UI needs a short Chinese sentence instead.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_field(self) -> str:
        """Value stored in jobs.error, e.g. 'no_audio: 该文件没有音轨，无法转录'."""
        return f"{self.code}: {self.message}"


def _as_int(value, default: int = 0) -> int:
    """把 sidecar 里读出来的任意值折成 int；坏数据不能拖垮续跑。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def result_to_json(result: AsrResult) -> dict:
    """契约 3：AsrResult 落盘形状，续跑靠 key 对齐。"""
    return {
        "key": result.key,
        "start_ms": int(getattr(result, "start_ms", 0) or 0),
        "end_ms": int(getattr(result, "end_ms", 0) or 0),
        "text": result.text,
        "tokens": [{"text": t.text, "start_ms": int(t.start_ms),
                    "end_ms": int(t.end_ms), "conf": float(t.conf)}
                   for t in result.tokens or []],
        "confidence": float(result.confidence or 0.0),
        "dur_s": float(result.dur_s or 0.0),
    }


def result_from_json(data) -> AsrResult | None:
    """读回 sidecar 里的一条结果；形状不对返回 None（宁可丢一段也别让续跑崩）。"""
    if not isinstance(data, dict):
        return None
    key = data.get("key")
    if not isinstance(key, str) or not key:
        return None
    tokens: list[Token] = []
    for row in data.get("tokens") or []:
        if not isinstance(row, dict):
            continue
        text = row.get("text")
        if not text:
            continue
        start_ms = _as_int(row.get("start_ms"))
        end_ms = _as_int(row.get("end_ms"), start_ms + 40)
        if end_ms <= start_ms:
            end_ms = start_ms + 40
        tokens.append(Token(text=str(text), start_ms=start_ms, end_ms=end_ms,
                            conf=_as_float(row.get("conf"), 1.0)))
    return AsrResult(
        key=key,
        text=str(data.get("text") or ""),
        tokens=tokens,
        confidence=_as_float(data.get("confidence")),
        dur_s=_as_float(data.get("dur_s")),
        start_ms=_as_int(data.get("start_ms")),
        end_ms=_as_int(data.get("end_ms")),
    )


def out_of_memory_types() -> tuple[type[BaseException], ...]:
    """契约 2 的 AsrOutOfMemory 定义在 runtime.py。

    这里在调用时解析而不是导入期绑定：runtime.py 由另一方并行改造，缺了它
    也只能让 OOM 识别退化，不能让整个 pipeline 模块导入失败。
    """
    cls = getattr(runtime_module, "AsrOutOfMemory", None)
    if isinstance(cls, type) and issubclass(cls, BaseException):
        return (cls,)
    return ()


def is_out_of_memory(exc: BaseException | None) -> bool:
    """判定一次失败是否属于显存不足（队列据此决定要不要降级重试）。"""
    if exc is None:
        return False
    if isinstance(exc, out_of_memory_types()):
        return True
    # 上层包装过（JobError）时也可能带着 code；契约允许按 code 判定
    if getattr(exc, "code", None) == "oom":
        return True
    # 最后一道兜底：runtime 还没包成 AsrOutOfMemory 时只有原始英文文本
    return "out of memory" in str(exc).lower()


def _supports_on_batch(fn: Callable) -> bool:
    """runtime.transcribe 是否已经长出了契约里的 on_batch 参数。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        # 反射不出来时按契约乐观调用：真出错也是 TypeError，能在测试里立刻暴露
        return True
    return "on_batch" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


class Pipeline:
    def __init__(self, paths: Paths, store: Store, settings: dict,
                 runtime: Runtime, emit: EmitFn | None = None,
                 lifecycle_lock: threading.RLock | None = None,
                 transcript_lock: threading.RLock | None = None,
                 cancel_requested: set[str] | None = None,
                 deleted: set[str] | None = None):
        self.paths = paths
        self.store = store
        self.settings = settings
        self.runtime = runtime
        self.emit = emit
        self._last_emit = 0.0
        # Shared with the HTTP layer: final commit vs cancel/delete/retry must
        # be adjudicated in one order, and transcript writes must not race the
        # editor's read-modify-write path.
        self._lifecycle_lock = lifecycle_lock or threading.RLock()
        self._transcript_lock = transcript_lock or threading.RLock()
        self._cancel_requested = cancel_requested if cancel_requested is not None else set()
        self._deleted = deleted if deleted is not None else set()
        # Results that reached the ASR callback but could not be persisted
        # (disk error) are kept in memory so an in-process OOM retry does not
        # silently recompute them.
        self._dirty_partials: dict[str, list[AsrResult]] = {}
        self._partial_warned: set[str] = set()
        # B2：显存不足时队列会给任务强制批大小（job_id -> batch）。只活在本进程
        # 内存里，进程重启后自然失效，不会让用户永久卡在 batch=1。
        self.batch_overrides: dict[str, int] = {}
        # 只警告一次「当前 runtime 没有 on_batch」，避免每批刷屏
        self._warned_no_on_batch = False
        # Cancel events of every in-flight job, so the shutdown path can stop
        # a running ffmpeg through the event it already watches.
        self._cancel_lock = threading.Lock()
        self._cancels: set[threading.Event] = set()

    def cancel_all(self) -> None:
        """Signal every in-flight job (graceful shutdown / engine exit)."""
        with self._cancel_lock:
            events = list(self._cancels)
        for event in events:
            event.set()

    # ------------------------------------------------------------------ api

    def run(self, job_id: str, cancel: threading.Event | None = None) -> dict | None:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        cancel = cancel or threading.Event()
        with self._cancel_lock:
            self._cancels.add(cancel)
        t0 = time.time()
        # A retry (fresh or resumable) must not inherit the previous attempt's
        # ffmpeg diagnostics file: it is only valid for the run that wrote it.
        self._remove_ffmpeg_log(job_id)
        try:
            # Claim under the lifecycle lock: a cancel that landed after the
            # worker dequeued the row must not be overwritten by an
            # unconditional status=processing update.
            with self._lifecycle_lock:
                row = self.store.get_job(job_id)
                if (not row or row.get("status") == "canceled"
                        or job_id in self._deleted):
                    raise Cancelled()
                if job_id in self._cancel_requested or cancel.is_set():
                    self.store.update_job(job_id, status="canceled", stage="canceled",
                                          finished_at=now_iso(), error=None)
                    raise Cancelled()
                stage = "load" if not self.runtime.ready else "probe"
                self.store.update_job(
                    job_id, status="processing", stage=stage, progress=0.0,
                    started_at=now_iso(), finished_at=None, error=None,
                )
            # C6: the first job loads the models before probing anything, so
            # the UI shows the long "load" stage instead of a stuck probe.
            if not self.runtime.ready:
                self._report(job_id, "load", P_LOAD[0],
                             "加载识别模型（首次约 20-40 秒）", force=True)
                self.runtime.load(
                    lambda p, m: self._report(job_id, "load", _lerp(P_LOAD, p), m))
            self._report(job_id, "probe", P_PROBE, "读取视频信息", force=True)
            transcript = self._run(job, t0, cancel)
            latest = self.store.get_job(job_id) or job
            if latest.get("auto_correct"):
                fixed = self._auto_correct(transcript)
                transcript["auto_corrected"] = fixed
            elapsed = round(time.time() - t0, 1)
            dur_s = max(0.001, (transcript["media"].get("duration_ms") or 0) / 1000.0)
            transcript["stats"]["elapsed_s"] = elapsed
            transcript["stats"]["rtf"] = round(elapsed / dur_s, 3)
            # The version is stamped by _save_transcript() under the
            # transcript lock, so it cannot be computed from a stale read
            # (see the comment there).
            issues = analysis.analyze(transcript, self.settings)
            summary = analysis.summarize(issues)
            with self._lifecycle_lock:
                if (cancel.is_set() or job_id in self._cancel_requested
                        or job_id in self._deleted):
                    raise Cancelled()
                self._save_transcript(job_id, transcript)
                self.store.update_job(
                    job_id, status="done", stage="done", progress=1.0,
                    rtf=transcript["stats"]["rtf"], elapsed_s=elapsed,
                    has_transcript=1, issues_json=json.dumps(summary, ensure_ascii=False),
                    finished_at=now_iso(), error=None,
                    duration_ms=transcript["media"].get("duration_ms") or 0,
                )
                # 结果已经落库，sidecar 没用了；失败/取消路径不走到这里，所以
                # 「崩在哪一层就保住到哪一层」的语义成立。
                self.discard_partial(job_id)
                self.batch_overrides.pop(job_id, None)
                self._dirty_partials.pop(job_id, None)
            self._report(job_id, "done", 1.0, "转录完成", force=True)
            return transcript
        except Cancelled:
            log.info("job %s cancelled", job_id)
            self.store.update_job(job_id, status="canceled", stage="canceled",
                                  finished_at=now_iso(), error=None)
            # 终态 stage 与 status 一致：旧实现 _report("done") 会把 canceled 覆盖成
            # done（progress 0），前端阶段标签语义错乱。
            self._report(job_id, "canceled", 0.0, "已取消", force=True)
            return None
        except JobError as e:
            # Business failure (e.g. no audio track): the row carries the code
            # and a Chinese sentence, never a raw English traceback.
            log.warning("job %s failed: %s (%s)", job_id, e.code, e.message)
            self.store.update_job(job_id, status="error", error=e.as_field(),
                                  finished_at=now_iso())
            self._report(job_id, "error", 0.0, e.message, force=True)
            raise
        except Exception as e:  # noqa: BLE001
            if is_out_of_memory(e):
                # 契约 2：显存不足要留一句可执行的中文，而不是 torch 的英文原文。
                # sidecar 故意保留——Worker 会据此降级重试并续跑已完成的段。
                log.warning("job %s 显存不足：%s", job_id, e)
                self.store.update_job(
                    job_id, status="error",
                    error=JobError("oom", OOM_MESSAGE).as_field(),
                    finished_at=now_iso(),
                )
                self._report(job_id, "error", 0.0, OOM_MESSAGE, force=True)
                raise
            log.exception("job %s failed", job_id)
            self.store.update_job(job_id, status="error", error=f"{type(e).__name__}: {e}",
                                  finished_at=now_iso())
            self._report(job_id, "error", 0.0, str(e), force=True)
            raise
        finally:
            with self._cancel_lock:
                self._cancels.discard(cancel)
            self._cleanup(job_id)

    def _auto_correct(self, transcript: dict) -> int:
        """Apply safe proof-reading fixes when the user opted in for this job."""
        if not transcript.get("sentences"):
            return 0
        issues = analysis.analyze(transcript, self.settings, include_lowconf=False)
        repeats = [i for i in issues if i.get("kind") == "repeat"]
        changed = 0
        by_sentence: dict[int, list[dict]] = {}
        for issue in repeats:
            by_sentence.setdefault(int(issue["sentence_id"]), []).append(issue)
        for sid, group in by_sentence.items():
            for issue in sorted(group, key=lambda i: int(i.get("start", 0)), reverse=True):
                try:
                    result = edit.apply_repeat(
                        transcript, sid, int(issue["start"]), int(issue["end"])
                    )
                    changed += int(result.get("changed", 0))
                except (KeyError, ValueError):
                    continue
        seen: set[tuple[str, str]] = set()
        for issue in issues:
            if issue.get("kind") != "suspicious":
                continue
            old = issue.get("text") or ""
            new = issue.get("suggestion") or ""
            if not old or not new or old == new or (old, new) in seen:
                continue
            seen.add((old, new))
            try:
                result = edit.apply_replace(transcript, old, new, scope="all")
                changed += int(result.get("changed", 0))
            except ValueError:
                continue
        return changed

    # -------------------------------------------------------------- internal

    def _run(self, job: dict, t0: float, cancel: threading.Event) -> dict:
        job_id = job["id"]
        src = Path(job["path"])
        # probe() returns has_audio=False for a missing/unreadable file, so a
        # user who moved or deleted the video after importing it used to get
        # the misleading "该文件没有音轨" error.  Say what actually happened.
        if not src.is_file():
            raise JobError(
                "file_missing",
                "素材文件不存在（可能已被移动或删除）：{}".format(src),
            )
        ffmpeg = self.paths.ffmpeg()
        ffprobe = self.paths.ffprobe()

        info = probe(ffprobe, src)
        if not info.has_audio:
            # probe 本身失败（损坏/受保护/格式异常）与「确实无音轨」要分开报，
            # 否则损坏文件也被误报成「没有音轨」。
            if info.probe_failed:
                raise JobError("probe_failed",
                               "无法读取媒体信息（文件可能损坏、受保护或格式异常）")
            raise JobError("no_audio", "该文件没有音轨，无法转录")
        duration_ms = info.duration_ms or 0
        # 续跑指纹用「时长 + 字节数」：换文件、改时长或改大小都算换了素材
        size_bytes = int(info.size_bytes or 0)
        if not size_bytes:
            try:
                size_bytes = src.stat().st_size
            except OSError:
                size_bytes = 0
        self.store.update_job(job_id, duration_ms=duration_ms, width=info.width,
                              height=info.height, name=job["name"])
        self._thumb(job_id, src, duration_ms)
        # 续跑判定必须在 probe 之后、ASR 之前：probe 给出的时长/字节数是唯一
        # 能证明「这条 sidecar 属于当前素材」的证据。指纹在这里一次定死，
        # 后面 wav 实测时长可能覆盖 duration_ms，不能让它污染比对。
        fingerprint = {
            "duration_ms": int(duration_ms),
            "size_bytes": int(size_bytes),
            "engine": VERSION,
            "asr": self._asr_signature(),
        }
        resumed = self._load_partial(job_id, fingerprint)

        # 1) audio ------------------------------------------------------
        wav = self.paths.cache_dir / f"{job_id}.wav"
        self._report(job_id, "extract", P_EXTRACT[0], "提取音轨", force=True)
        # A truncated wav left by an interrupted run used to be reused just
        # because it was bigger than the 44-byte header; the job then claimed
        # done on a fraction of the audio.  Validate duration + media
        # fingerprint before trusting the cache.
        if not self._wav_reusable(wav, fingerprint):
            extract_audio(
                ffmpeg, src, wav, duration_ms,
                on_progress=lambda p: self._report(job_id, "extract", _lerp(P_EXTRACT, p),
                                                   "提取音轨"),
                cancel=cancel,
            )
            self._verify_wav(wav, duration_ms)
            self._write_wav_fingerprint(wav, fingerprint)
        if cancel.is_set():
            raise Cancelled()

        # 2) VAD --------------------------------------------------------
        with sf.SoundFile(str(wav)) as f:
            sr = f.samplerate
            total_frames = f.frames
            real_duration_ms = int(total_frames / max(sr, 1) * 1000)
            duration_ms = duration_ms or real_duration_ms
            window_ms = max(5, int(self.settings.get("window_minutes", 30))) * 60_000
            segments = self._vad(job_id, f, sr, total_frames, window_ms, cancel)
            if cancel.is_set():
                raise Cancelled()

            # 4) ASR ----------------------------------------------------
            items = self._make_items(f, segments)
            results = self._transcribe(
                job_id,
                items,
                {r.key: r for r in (resumed or [])},
                fingerprint,
                cancel,
            )
            tokens: list[Token] = []
            for r in results:
                # the model gives one confidence per ASR segment; propagate it
                # to the tokens so sentence/word confidence is meaningful
                if r.confidence:
                    for t in r.tokens:
                        t.conf = float(r.confidence)
                tokens.extend(r.tokens)
            tokens.sort(key=lambda t: (t.start_ms, t.end_ms))

        if cancel.is_set():
            raise Cancelled()

        # 5) punctuation ------------------------------------------------
        self._report(job_id, "punc", P_PUNC[0], "智能断句", force=True)
        sentences = self.runtime.punctuate(
            tokens,
            progress=lambda p, m: self._report(job_id, "punc", _lerp(P_PUNC, p), m),
            cancelled=cancel.is_set,
        )

        # 6) transcript -------------------------------------------------
        self._report(job_id, "finalize", 0.99, "整理结果", force=True)
        out_sentences = []
        for i, s in enumerate(sentences):
            text = s.get("text") or ""
            words = s.get("words") or []
            if not text.strip():
                continue
            start = int(s.get("start_ms") or (words[0]["start_ms"] if words else 0))
            end = int(s.get("end_ms") or (words[-1]["end_ms"] if words else start))
            out_sentences.append({
                "id": i,
                "start_ms": start,
                "end_ms": max(end, start + 200),
                "text": text,
                "conf": round(float(s.get("confidence") or 0.0), 3),
                "words": [{"text": w["text"], "start_ms": int(w["start_ms"]),
                           "end_ms": int(w["end_ms"])} for w in words if w.get("text")],
                "edited": False,
            })
        # renumber after dropping empties
        for i, s in enumerate(out_sentences):
            s["id"] = i
        # Fillers and stutters are removed before segmentation so the editor,
        # the preview and the export all read the same clean text.
        out_sentences = cleanup.clean_sentences(out_sentences, self.settings)
        elapsed = round(time.time() - t0, 1)
        transcript = {
            "job_id": job_id,
            "version": 1,
            "seg_version": SEG_VERSION,
            "media": {
                "name": job["name"],
                "path": str(src),
                "duration_ms": duration_ms,
                "width": info.width,
                "height": info.height,
            },
            "sentences": out_sentences,
            "dismissed": [],
            "stats": {
                # SPEC §3.2: content characters only (count_chars), same as the
                # editor/segmenter/export paths.
                "chars": sum(count_chars(s["text"]) for s in out_sentences),
                "sentences": len(out_sentences),
                "duration_ms": duration_ms,
                "rtf": 0.0,
                "elapsed_s": elapsed,
            },
        }
        # Split long ASR sentences before they reach the editor/export.  This
        # is the same implementation the exporter and old-task migration use.
        segment_transcript(
            transcript,
            max_chars=int(self.settings.get("subtitle_max_chars", 22)),
            merge_gap_ms=int(self.settings.get("subtitle_merge_gap_ms", 900)),
            force=True,
        )
        transcript["seg_version"] = SEG_VERSION
        return transcript

    # ------------------------------------------------- 增量落盘 / 续跑

    def partial_path(self, job_id: str) -> Path:
        """ASR 增量结果 sidecar：<data_dir>/partial/<job_id>.json。"""
        return self.paths.data_dir / PARTIAL_DIRNAME / f"{job_id}.json"

    def discard_partial(self, job_id: str) -> bool:
        """删掉 sidecar，返回是否真的删了一个文件。

        成功收尾、换素材、用户要求 fresh 重跑、删任务都会走这里；删不掉只是
        留下垃圾文件，绝不能让调用方失败。
        """
        path = self.partial_path(job_id)
        try:
            if path.is_file():
                path.unlink()
                log.info("已删除中间结果 %s", path)
            return True
        except OSError:
            log.warning("删除中间结果失败：%s", path, exc_info=True)
            return False

    def _write_partial(self, job_id: str, media: dict, results: list[AsrResult]) -> None:
        """原子写 sidecar：唯一临时文件 + os.replace，进程被杀只丢最后一批。"""
        path = self.partial_path(job_id)
        payload = {
            "v": PARTIAL_VERSION,
            "media": dict(media or {}),
            "results": [result_to_json(r) for r in results],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # 唯一临时名（旧固定 .tmp 会被并发/崩溃残留互相覆盖）；失败也清掉临时文件。
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _load_partial(self, job_id: str, fingerprint: dict) -> list[AsrResult] | None:
        """读出可续跑的已完成结果；媒体/模型指纹不一致就地丢弃并返回 None。"""
        path = self.partial_path(job_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("中间结果读不出来，丢弃后从头识别：%s", path, exc_info=True)
            self.discard_partial(job_id)
            return None
        if not isinstance(raw, dict) or _as_int(raw.get("v")) != PARTIAL_VERSION:
            version = raw.get("v") if isinstance(raw, dict) else "?"
            log.warning("中间结果版本不认识（v=%s），丢弃后从头识别：%s", version, path)
            self.discard_partial(job_id)
            return None
        media = raw.get("media") if isinstance(raw.get("media"), dict) else {}

        def same(stored, expected) -> bool:
            try:
                return int(stored) == int(expected)
            except (TypeError, ValueError):
                return str(stored) == str(expected)

        mismatch = [key for key, value in fingerprint.items()
                    if not same(media.get(key), value)]
        if mismatch:
            # 换了文件 / 时长 / 模型或切段参数：旧 key 无法再对齐，只能从头跑
            log.warning(
                "中间结果与当前指纹不一致（%s），丢弃后从头识别：%s",
                ", ".join(f"{key}: {media.get(key)}→{fingerprint[key]}" for key in mismatch),
                path,
            )
            self.discard_partial(job_id)
            return None
        results: list[AsrResult] = []
        for row in raw.get("results") or []:
            result = result_from_json(row)
            if result is not None:
                results.append(result)
        log.info("找到可续跑的中间结果：%s（%d 段）", path, len(results))
        return results

    def _transcribe(self, job_id: str, items: list[AsrItem],
                    done_by_key: dict[str, AsrResult], media: dict,
                    cancel: threading.Event) -> list[AsrResult]:
        """跑 ASR 并按时序合并；done_by_key 里的段直接复用（续跑）。"""
        item_by_key = {it.key: it for it in items}
        # Same-process retry after an OOM: results whose sidecar write failed
        # are still in memory, so do not recompute them.
        dirty = self._dirty_partials.pop(job_id, None)
        if dirty:
            merged_dirty = dict(done_by_key)
            for result in dirty:
                merged_dirty[result.key] = result
            done_by_key = merged_dirty
            log.info("job %s 恢复 %d 段未落盘的中间结果", job_id, len(dirty))
        # A key alone is not proof that the cached result belongs to this
        # segment: s0 may now start at 5 s instead of 0.1 s.  Reuse only when
        # the stored boundary matches the current item.
        reusable: dict[str, AsrResult] = {}
        dropped = 0
        for key, result in done_by_key.items():
            item = item_by_key.get(key)
            if item is None:
                dropped += 1
                continue
            start = _as_int(getattr(result, "start_ms", 0))
            end = _as_int(getattr(result, "end_ms", 0))
            if (start or end) and (
                abs(start - item.start_ms) > 1 or abs(end - item.end_ms) > 1
            ):
                dropped += 1
                continue
            reusable[key] = result
        if dropped:
            log.warning("job %s 有 %d 段中间结果与当前切段边界不符，已丢弃",
                        job_id, dropped)
        done_by_key = reusable
        total = max(len(items), 1)
        done_count = sum(1 for it in items if it.key in done_by_key)
        todo = [it for it in items if it.key not in done_by_key]
        if done_count:
            message = f"续跑：已完成 {done_count}/{total} 段"
            log.info("job %s %s", job_id, message)
            self._report(job_id, "asr", P_ASR[0], message, force=True)
        else:
            self._report(job_id, "asr", P_ASR[0],
                         f"开始识别 {len(items)} 段语音", force=True)
        if not todo:
            # 上一次已经把所有段都识别完，只差收尾：别再进模型白跑一遍
            log.info("job %s 全部 %d 段都已完成，跳过 ASR", job_id, total)
            return [done_by_key[it.key] for it in items if it.key in done_by_key]

        accumulated: list[AsrResult] = [done_by_key[it.key] for it in items
                                        if it.key in done_by_key]

        def on_batch(batch: list[AsrResult]) -> None:
            """契约 1：每完成一批回调一次；落盘失败只警告，绝不影响识别。"""
            accumulated.extend(batch)
            if job_id in self._deleted or job_id in self._cancel_requested:
                return
            try:
                self._write_partial(job_id, media, accumulated)
                self._dirty_partials.pop(job_id, None)
            except (OSError, TypeError, ValueError):
                # Keep the un-persisted results in memory: an in-process OOM
                # retry used to recompute everything after the last checkpoint
                # while still reporting the progress as saved.
                self._dirty_partials[job_id] = list(accumulated)
                log.warning("中间结果落盘失败（进度暂存内存）：%s",
                            self.partial_path(job_id), exc_info=True)
                if job_id not in self._partial_warned:
                    self._partial_warned.add(job_id)
                    if self.emit:
                        try:
                            self.emit({"type": "toast", "level": "warn",
                                       "message": "中间结果落盘失败，进度暂存在内存中；请检查磁盘空间"})
                        except Exception:  # noqa: BLE001
                            pass

        def progress(p: float, message: str) -> None:
            # 续跑时把本轮进度折回整体比例，进度条不会从 0 重新爬
            self._report(job_id, "asr",
                         _lerp(P_ASR, (done_count + p * len(todo)) / total), message)

        fresh = self._call_transcribe(todo, self._batch_size(job_id), progress,
                                      cancel.is_set, on_batch)
        merged: dict[str, AsrResult] = dict(done_by_key)
        for result in fresh:
            merged[result.key] = result
        ordered: list[AsrResult] = []
        missing: list[str] = []
        for it in items:
            result = merged.get(it.key)
            if result is None:
                missing.append(it.key)
                continue
            ordered.append(result)
        if missing:
            # A "done" row with a hole is worse than a failed row: the job
            # would report success while speech is silently absent from the
            # subtitles.  Fail instead; the sidecar keeps every finished batch,
            # so a retry resumes instead of recomputing them.
            log.warning("job %s 缺少 %d/%d 段识别结果：%s", job_id, len(missing),
                        len(items), ", ".join(missing[:5]))
            raise RuntimeError(
                "语音识别未返回 {} / {} 段结果，任务中止（已完成的段落已保存，"
                "可直接重试续跑）".format(len(missing), len(items)))
        self._dirty_partials.pop(job_id, None)
        return ordered

    def _call_transcribe(self, items: list[AsrItem], batch_size: int,
                         progress: Callable[[float, str], None],
                         cancelled: Callable[[], bool],
                         on_batch: Callable[[list[AsrResult]], None]) -> list[AsrResult]:
        """契约 1 的唯一调用点。

        runtime.py 由另一方并行改造；万一那版 transcribe 还没长 on_batch，就
        退化成不带增量回调的调用——识别照跑，只是少了落盘保护。
        """
        kwargs: dict = {"batch_size": batch_size, "progress": progress,
                        "cancelled": cancelled}
        if _supports_on_batch(self.runtime.transcribe):
            kwargs["on_batch"] = on_batch
        elif not self._warned_no_on_batch:
            self._warned_no_on_batch = True
            log.warning("当前 runtime.transcribe 没有 on_batch 参数，"
                        "本次不写中间结果（识别本身不受影响）")
        return self.runtime.transcribe(items, **kwargs)

    def _batch_size(self, job_id: str) -> int:
        """B2：因显存不足降过级的任务强制 batch=1，其余按设置值走。"""
        requested = _as_int(self.settings.get("batch_size", 6)) or 6
        override = _as_int(self.batch_overrides.get(job_id, NO_BATCH_OVERRIDE),
                           NO_BATCH_OVERRIDE)
        return max(1, min(requested, override))

    # ------------------------------------------------------------------ vad

    # A whole-file VAD pass is exact (no windows, no dropped boundary speech)
    # and costs ~1.7s for 12 minutes of audio.  Beyond five hours the raw
    # int16 buffer starts to matter, so fall back to context windows.
    MAX_GLOBAL_MS = 5 * 60 * 60 * 1000

    def _vad(self, job_id: str, f: sf.SoundFile, sr: int, total_frames: int,
             window_ms: int, cancel: threading.Event) -> list[tuple[int, int]]:
        duration_ms = int(total_frames / max(sr, 1) * 1000)
        if duration_ms <= self.MAX_GLOBAL_MS:
            self._report(job_id, "vad", P_VAD[0], "检测语音", force=True)
            f.seek(0)
            audio = f.read(total_frames, dtype="int16", always_2d=False)
            audio = np.asarray(audio, dtype=np.int16).reshape(-1)
            segments = [(max(0, int(s)), min(duration_ms, int(e)))
                        for s, e in self.runtime.detect(audio)]
            if cancel.is_set():
                raise Cancelled()
            self._report(job_id, "vad", P_VAD[1], f"检测到 {len(segments)} 段语音")
        else:
            segments = self._vad_windows(job_id, f, sr, total_frames, window_ms, cancel)
        return self._normalize_segments(segments)

    @staticmethod
    def _normalize_segments(segments: list[tuple[int, int]]) -> list[tuple[int, int]]:
        segments.sort()
        merged: list[tuple[int, int]] = []
        for s, e in segments:
            if e - s < MIN_SEGMENT_MS:
                continue
            if merged and s - merged[-1][1] < 200:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        out: list[tuple[int, int]] = []
        for s, e in merged:
            if e - s > MAX_SEGMENT_MS:
                while e - s > MAX_SEGMENT_MS:
                    out.append((s, s + MAX_SEGMENT_MS))
                    s += MAX_SEGMENT_MS
                if 0 < e - s < MIN_SEGMENT_MS and out and out[-1][1] == s:
                    # A fixed 28 s stride can leave a <160 ms tail that the
                    # MIN_SEGMENT_MS filter below would drop, cutting up to
                    # 159 ms of speech out of every long continuous run.  Fold
                    # that tail into the previous piece instead; the result
                    # stays below MAX_SEGMENT_MS + MIN_SEGMENT_MS (28.16 s),
                    # far under the model's 60 s limit.
                    out[-1] = (out[-1][0], e)
                    continue
            if e - s >= MIN_SEGMENT_MS:
                out.append((s, e))
        return out

    def _vad_windows(self, job_id: str, f: sf.SoundFile, sr: int, total_frames: int,
                     window_ms: int, cancel: threading.Event) -> list[tuple[int, int]]:
        win_frames = int(window_ms / 1000 * sr)
        ctx_frames = int(WINDOW_CTX_MS / 1000 * sr)
        segments: list[tuple[int, int]] = []
        total_ms = int(round(total_frames * 1000 / sr))
        pos = 0
        windows = max(1, (total_frames + win_frames - 1) // win_frames)
        for wi in range(windows):
            if cancel.is_set():
                raise Cancelled()
            core_start = pos
            core_end = min(total_frames, pos + win_frames)
            read_start = max(0, core_start - ctx_frames)
            read_end = min(total_frames, core_end + ctx_frames)
            f.seek(read_start)
            audio = f.read(read_end - read_start, dtype="int16", always_2d=False)
            audio = np.asarray(audio, dtype=np.int16).reshape(-1)
            core_start_ms = int(round(core_start * 1000 / sr))
            core_end_ms = int(round(core_end * 1000 / sr))
            offset_ms = int(round(read_start * 1000 / sr))
            for s_ms, e_ms in self.runtime.detect(audio):
                s_abs = offset_ms + int(s_ms)
                e_abs = offset_ms + int(e_ms)
                # Assign every detected segment to exactly one core window:
                # clip it to [core_start, core_end] and let _normalize_segments
                # merge the touching halves.  Dropping segments that started
                # in the 8 s context lost speech at every window boundary
                # (36 s on a 5 h + 1 s file).
                s_abs = max(s_abs, core_start_ms)
                e_abs = min(e_abs, core_end_ms)
                if e_abs <= s_abs:
                    continue
                segments.append((max(0, s_abs), min(total_ms, e_abs)))
            pos = core_end
            self._report(job_id, "vad", _lerp(P_VAD, (wi + 1) / windows),
                         f"检测语音 {wi + 1}/{windows}")
        return segments

    def _make_items(self, f: sf.SoundFile, segments: list[tuple[int, int]]) -> list[AsrItem]:
        sr = f.samplerate
        items: list[AsrItem] = []
        for i, (s_ms, e_ms) in enumerate(segments):
            f.seek(int(s_ms / 1000 * sr))
            audio = f.read(int((e_ms - s_ms) / 1000 * sr), dtype="int16", always_2d=False)
            audio = np.asarray(audio, dtype=np.int16).reshape(-1)
            if audio.size < sr // 10:
                continue
            items.append(AsrItem(key=f"s{i}", start_ms=s_ms, end_ms=e_ms, audio=audio))
        return items

    # ------------------------------------------------------- cache / versions

    def _asr_signature(self) -> str:
        keys = ("device", "half", "beam_size", "window_minutes")
        return ";".join(f"{k}={self.settings.get(k)}" for k in keys)

    def _next_version(self, job_id: str) -> int:
        path = self.paths.transcript_dir / f"{job_id}.json"
        try:
            if path.is_file():
                old = json.loads(path.read_text(encoding="utf-8"))
                return int(old.get("version") or 0) + 1
        except (OSError, ValueError, TypeError):
            pass
        return 1

    @staticmethod
    def _wav_duration_ms(wav: Path) -> int:
        try:
            with sf.SoundFile(str(wav)) as f:
                return int(f.frames / max(f.samplerate, 1) * 1000)
        except Exception:  # noqa: BLE001 - unreadable cache is a cache miss
            return -1

    @staticmethod
    def _wav_meta_path(wav: Path) -> Path:
        return wav.with_name(wav.name + ".meta.json")

    def _wav_reusable(self, wav: Path, fingerprint: dict) -> bool:
        if not (wav.exists() and wav.stat().st_size > 44):
            return False
        try:
            meta = json.loads(self._wav_meta_path(wav).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if (str(meta.get("duration_ms")) != str(fingerprint.get("duration_ms"))
                or str(meta.get("size_bytes")) != str(fingerprint.get("size_bytes"))):
            return False
        expected = _as_int(fingerprint.get("duration_ms"))
        actual = self._wav_duration_ms(wav)
        if actual < 0:
            return False
        if expected > 0:
            tolerance = max(2000, int(expected * 0.001))
            if abs(actual - expected) > tolerance:
                return False
        return True

    def _write_wav_fingerprint(self, wav: Path, fingerprint: dict) -> None:
        try:
            self._wav_meta_path(wav).write_text(
                json.dumps({"duration_ms": fingerprint.get("duration_ms"),
                            "size_bytes": fingerprint.get("size_bytes")},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            log.warning("音轨缓存指纹写入失败：%s", wav, exc_info=True)

    def _verify_wav(self, wav: Path, duration_ms: int) -> None:
        actual = self._wav_duration_ms(wav)
        if actual < 0:
            raise JobError("extract_failed", "音轨提取失败：生成的音频无法读取")
        if duration_ms > 0:
            tolerance = max(2000, int(duration_ms * 0.001))
            if abs(actual - duration_ms) > tolerance:
                raise JobError("extract_incomplete", "音轨提取不完整，请重试")

    # --------------------------------------------------------------- misc

    def _thumb(self, job_id: str, src: Path, duration_ms: int) -> None:
        dst = self.paths.thumb_dir / f"{job_id}.jpg"
        try:
            make_thumb(self.paths.ffmpeg(), src, dst,
                       at_s=min(max(duration_ms / 1000 * 0.08, 1.0), 30.0))
            self.store.update_job(job_id, thumb_path=str(dst) if dst.exists() else None)
        except Exception:  # noqa: BLE001
            log.warning("thumbnail failed for %s", job_id, exc_info=True)

    def _save_transcript(self, job_id: str, transcript: dict) -> None:
        """Write the transcript and stamp a version derived from disk.

        The version must be read *under the transcript lock*: reading it in
        run() before the lock left a window where an editor saved version N+1
        and the worker then published N+1 with different content too, so CAS
        clients could no longer tell the two apart (a lost edit).
        """
        path = self.paths.transcript_dir / f"{job_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Share the editor's lock and use a unique staging name: the old fixed
        # .json.tmp could be clobbered by a concurrent save.
        with self._transcript_lock:
            transcript["version"] = self._next_version(job_id)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f"{job_id}.", suffix=".json.tmp", dir=str(path.parent)
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(transcript, handle, ensure_ascii=False)
                for attempt in range(6):
                    try:
                        os.replace(tmp, path)
                        break
                    except (PermissionError, FileNotFoundError):
                        if attempt == 5:
                            raise
                        if not tmp.exists():
                            tmp.write_text(json.dumps(transcript, ensure_ascii=False),
                                           encoding="utf-8")
                        time.sleep(0.08 * (attempt + 1))
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            self.store.update_job(job_id, transcript_path=str(path))

    @staticmethod
    def _ffmpeg_log_path(cache_dir: Path, job_id: str) -> Path:
        """<data_dir>/cache/<job>.wav.ffmpeg.log (media.extract_audio's name)."""
        wav = cache_dir / f"{job_id}.wav"
        return wav.with_name(wav.name + ".ffmpeg.log")

    def _remove_ffmpeg_log(self, job_id: str) -> None:
        """Drop a stale ffmpeg diagnostics file; never fail the job over it."""
        path = self._ffmpeg_log_path(self.paths.cache_dir, job_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("删除 ffmpeg 诊断日志失败：%s", path, exc_info=True)

    def _cleanup(self, job_id: str) -> None:
        # The ffmpeg log is per-attempt: remove it on done / canceled / error.
        # keep_wav protects the decoded wav only, not this diagnostics file.
        self._remove_ffmpeg_log(job_id)
        if self.settings.get("keep_wav"):
            return
        wav = self.paths.cache_dir / f"{job_id}.wav"
        for path in (wav, self._wav_meta_path(wav)):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    @staticmethod
    def _public_job(job: dict | None) -> dict | None:
        """Same shape as server.State.public_job: tokens never ride the URL.

        _report used to push the raw sqlite row, so a websocket-driven job had
        no thumb_url/media_url (the thumbnail vanished until the next /jobs
        poll) and a delete that raced the progress event pushed job=null,
        which made the frontend's upsertJobs() throw.
        """
        if not job:
            return None
        job = dict(job)
        if job.get("thumb_path"):
            job_id = job["id"]
            job["thumb_url"] = f"/api/jobs/{job_id}/thumb"
            job["media_url"] = f"/api/jobs/{job_id}/media"
        return job

    def _report(self, job_id: str, stage: str, progress: float, message: str,
                force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < 0.3:
            return
        self._last_emit = now
        progress = round(max(0.0, min(1.0, progress)), 4)
        try:
            self.store.update_job(job_id, stage=stage, progress=progress)
        except Exception:  # noqa: BLE001
            log.debug("progress update failed", exc_info=True)
        if self.emit:
            try:
                job = self._public_job(self.store.get_job(job_id))
                if job is not None:
                    self.emit({"type": "job", "job": job, "message": message})
            except Exception:  # noqa: BLE001
                log.debug("emit failed", exc_info=True)
