"""Regression tests for the 2026-11-18 core-pipeline audit.

Pinned defects (each test would fail on the pre-fix code):

* Pipeline._normalize_segments dropped a sub-160 ms tail left by the fixed
  28 s stride, cutting up to 159 ms of continuous speech out of the subtitle;
* Runtime._decode fabricated an empty result for ASR utterances the model
  never returned (FireRedAsr2 swallows its own exceptions), so the sidecar
  marked those segments "done" and speech vanished without an error;
* Pipeline._transcribe silently skipped segments with no result, letting the
  job finish as "done" with a hole in the subtitles;
* the regenerated transcript version was read before the transcript lock was
  taken, so a concurrent editor save could publish the same version with
  different content;
* analysis.find_repeats flagged legitimate measure-word reduplications
  ("个个人" / "个个都是") and auto-correct deleted one 个, contradicting the
  documented _PREFIX_STOP intent.

Design: no torch / CUDA / model / ffmpeg is touched; the ASR and pipeline
tests use stubs.  Scratch directories live under
<project>/_scratch/<date>/engine-pipeline and are removed in tearDown.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import unittest
import uuid
from pathlib import Path

import numpy as np

from voicetrace_engine import analysis, media
from voicetrace_engine.config import DEFAULT_SETTINGS, Paths
from voicetrace_engine.pipeline import JobError, Pipeline
from voicetrace_engine.runtime import AsrItem, AsrResult, Cancelled, Runtime

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_RATE = 16000


# ------------------------------------------------------------------ helpers


def scratch_dir(tag: str) -> Path:
    base = PROJECT_ROOT / "_scratch" / time.strftime("%Y-%m-%d") / "engine-pipeline"
    path = base / "{}-{}".format(tag, uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_scratch(path: Path) -> None:
    target = Path(path).resolve()
    if PROJECT_ROOT not in target.parents or "_scratch" not in target.parts:
        return
    shutil.rmtree(target, ignore_errors=True)
    try:
        target.parent.rmdir()
    except OSError:
        pass


def make_item(key: str, start_ms: int, end_ms: int) -> AsrItem:
    return AsrItem(key=key, start_ms=start_ms, end_ms=end_ms,
                   audio=np.zeros(SAMPLE_RATE // 10, dtype=np.int16))


def make_sentence(sid: int, text: str) -> dict:
    return {"id": sid, "text": text, "start_ms": 0, "end_ms": 1000,
            "conf": 0.9, "words": [], "edited": False}


class StoreStub:
    """Only the two Store calls the pipeline touches outside the DB tests."""

    def __init__(self, rows: dict | None = None) -> None:
        self.rows = dict(rows or {})

    def get_job(self, job_id: str):
        return self.rows.get(job_id)

    def update_job(self, job_id: str, **fields) -> None:
        row = self.rows.setdefault(job_id, {"id": job_id})
        row.update(fields)


class RuntimeStub:
    ready = True

    def __init__(self, keep: int) -> None:
        self.keep = keep
        self.seen: list[list[str]] = []

    def transcribe(self, items, batch_size=6, progress=None, cancelled=None,
                   on_batch=None):
        self.seen.append([it.key for it in items])
        out = [AsrResult(key=it.key, text="ok", start_ms=it.start_ms,
                         end_ms=it.end_ms) for it in items[:self.keep]]
        if on_batch is not None:
            on_batch(out)
        return out


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch_dir("audit-core")
        self.paths = Paths(data_dir=self.tmp / "data", models_dir=self.tmp / "models")
        self.paths.ensure()
        self.store = StoreStub()

    def tearDown(self):
        remove_scratch(self.tmp)

    def make_pipeline(self, runtime=None, **kwargs) -> Pipeline:
        return Pipeline(self.paths, self.store, dict(DEFAULT_SETTINGS),
                        runtime or RuntimeStub(keep=99), **kwargs)


class MissingMediaFileTests(PipelineCase):
    """A moved/deleted video must not masquerade as "no audio track"."""

    def test_missing_file_is_reported_as_file_missing(self):
        self.store.rows["job-1"] = {
            "id": "job-1",
            "path": str(self.tmp / "gone.mp4"),
            "name": "gone.mp4",
        }
        pipeline = self.make_pipeline()
        with self.assertRaises(JobError) as ctx:
            pipeline.run("job-1")
        self.assertEqual(ctx.exception.code, "file_missing")
        self.assertIn("已被移动或删除", ctx.exception.message)
        row = self.store.rows["job-1"]
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["error"].startswith("file_missing:"), row["error"])


# ------------------------------------------------------- A. VAD split tails


class VadSplitTailTests(unittest.TestCase):
    """A: the 28 s stride must not throw away a sub-160 ms tail."""

    def test_short_tail_is_folded_into_the_previous_piece(self):
        for end in (28001, 28100, 28150, 28159):
            out = Pipeline._normalize_segments([(0, end)])
            self.assertEqual(out, [(0, end)], "tail of {} ms was lost".format(end - 28000))

    def test_boundary_at_minimum_segment_is_unchanged(self):
        self.assertEqual(Pipeline._normalize_segments([(0, 28160)]),
                         [(0, 28000), (28000, 28160)])

    def test_normal_split_still_works(self):
        self.assertEqual(Pipeline._normalize_segments([(0, 40000)]),
                         [(0, 28000), (28000, 40000)])
        self.assertEqual(Pipeline._normalize_segments([(0, 56000)]),
                         [(0, 28000), (28000, 56000)])

    def test_no_piece_exceeds_the_hard_cap(self):
        for end in (28159, 28200, 56100, 84050):
            for start, stop in Pipeline._normalize_segments([(0, end)]):
                self.assertLessEqual(stop - start, 28000 + 159, (start, stop))

    def test_standalone_short_blip_is_still_dropped(self):
        self.assertEqual(Pipeline._normalize_segments([(0, 100)]), [])
        self.assertEqual(Pipeline._normalize_segments([(0, 100), (500, 1000)]),
                         [(500, 1000)])


# --------------------------------------------------- B. runtime missing uttids


class _UttidAsr:
    def __init__(self, keep: int) -> None:
        self.keep = keep
        self.calls = 0

    def transcribe(self, keys, audios):
        self.calls += 1
        return [{"uttid": key, "text": "好", "timestamp": [["好", 0.1, 0.2]],
                 "confidence": 0.9} for key in keys[:self.keep]]


class RuntimeMissingResultTests(PipelineCase):
    """B: a short result list must fail the batch, not fake empty segments."""

    def make_runtime(self, keep: int) -> Runtime:
        runtime = Runtime(self.paths, dict(DEFAULT_SETTINGS), use_gpu=False)
        runtime._asr = _UttidAsr(keep)
        runtime._torch = None
        runtime.device = "cpu"
        return runtime

    def test_empty_result_list_raises(self):
        runtime = self.make_runtime(keep=0)
        with self.assertRaises(RuntimeError) as ctx:
            runtime.transcribe([make_item("s0", 0, 1000),
                                make_item("s1", 1000, 2000)], batch_size=2)
        self.assertIn("语音识别未返回", str(ctx.exception))

    def test_partial_result_list_raises_and_skips_on_batch(self):
        runtime = self.make_runtime(keep=1)
        batches: list[list[str]] = []
        with self.assertRaises(RuntimeError):
            runtime.transcribe([make_item("s0", 0, 1000), make_item("s1", 1000, 2000)],
                               batch_size=2,
                               on_batch=lambda b: batches.append([r.key for r in b]))
        self.assertEqual(batches, [], "a failed batch must not be checkpointed")

    def test_complete_result_list_is_unchanged(self):
        runtime = self.make_runtime(keep=2)
        results = runtime.transcribe([make_item("s0", 0, 1000),
                                      make_item("s1", 1000, 2000)], batch_size=2)
        self.assertEqual([r.key for r in results], ["s0", "s1"])
        self.assertEqual([r.text for r in results], ["好", "好"])


# ------------------------------------------------ C. pipeline missing results


class PipelineMissingResultTests(PipelineCase):
    """C: a job must not finish "done" when ASR results are missing."""

    def test_missing_segment_fails_instead_of_being_skipped(self):
        pipeline = self.make_pipeline(RuntimeStub(keep=1))
        items = [make_item("s0", 0, 1000), make_item("s1", 1000, 2000)]
        with self.assertRaises(RuntimeError) as ctx:
            pipeline._transcribe("job-c", items, {}, {"duration_ms": 2000},
                                 threading.Event())
        self.assertIn("段结果", str(ctx.exception))
        self.assertIn("已完成的段落已保存", str(ctx.exception))

    def test_complete_results_keep_vad_order(self):
        runtime = RuntimeStub(keep=99)
        pipeline = self.make_pipeline(runtime)
        items = [make_item("s0", 0, 1000), make_item("s1", 1000, 2000)]
        results = pipeline._transcribe("job-ok", items, {}, {}, threading.Event())
        self.assertEqual([r.key for r in results], ["s0", "s1"])
        self.assertEqual(runtime.seen, [["s0", "s1"]])


# ------------------------------------------------------ D. transcript version


class TranscriptVersionTests(PipelineCase):
    """D: version stamping happens at save time, under the transcript lock."""

    def transcript_path(self, job_id: str) -> Path:
        return self.paths.transcript_dir / "{}.json".format(job_id)

    def test_version_advances_from_what_is_on_disk(self):
        path = self.transcript_path("job-v")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 3, "sentences": []}), encoding="utf-8")
        pipeline = self.make_pipeline()
        transcript = {"job_id": "job-v", "sentences": [make_sentence(0, "甲")]}
        pipeline._save_transcript("job-v", transcript)
        self.assertEqual(transcript["version"], 4)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 4)

    def test_first_save_starts_at_version_one(self):
        pipeline = self.make_pipeline()
        transcript = {"job_id": "job-new", "sentences": []}
        pipeline._save_transcript("job-new", transcript)
        self.assertEqual(transcript["version"], 1)

    def test_version_is_read_while_holding_the_transcript_lock(self):
        pipeline = self.make_pipeline()
        real_next = pipeline._next_version
        started = threading.Event()
        probe_done = threading.Event()
        observed: dict[str, bool] = {}

        def spy(job_id: str) -> int:
            started.set()
            probe_done.wait(5.0)
            observed["called"] = True
            return real_next(job_id)

        def probe() -> None:
            started.wait(5.0)
            acquired = pipeline._transcript_lock.acquire(blocking=False)
            observed["acquired"] = bool(acquired)
            if acquired:
                pipeline._transcript_lock.release()
            probe_done.set()

        pipeline._next_version = spy  # type: ignore[method-assign]
        helper = threading.Thread(target=probe, name="version-lock-probe", daemon=True)
        helper.start()
        try:
            pipeline._save_transcript("job-lock", {"job_id": "job-lock", "sentences": []})
        finally:
            probe_done.set()
            helper.join(5.0)
        self.assertTrue(observed.get("called"),
                        "the version stamp must go through _next_version")
        self.assertIs(observed.get("acquired"), False,
                      "version was read outside the transcript lock")


# ------------------------------------------------ E. measure-word repetitions


class MeasureWordRepeatTests(PipelineCase):
    """E: 个个人 / 个个都是 must survive auto-correct; real stutters still flagged."""

    def test_measure_word_reduplications_are_not_repeats(self):
        for text in ("个个人", "个个都是好样的", "三个个人", "这个个人", "每个个人"):
            issues = analysis.find_repeats([make_sentence(0, text)])
            self.assertEqual(issues, [], text)

    def test_real_stutters_are_still_detected(self):
        expected = {
            "我我觉得": ("我我", "我"),
            "老老师": ("老老", "老"),
            "这个这个这个真好": ("这个这个这个", "这个"),
        }
        for text, (span, suggestion) in expected.items():
            issues = analysis.find_repeats([make_sentence(0, text)])
            self.assertTrue(issues, text)
            self.assertIn((span, suggestion),
                          {(i["text"], i["suggestion"]) for i in issues}, text)

    def test_auto_correct_leaves_measure_word_text_alone(self):
        pipeline = self.make_pipeline()
        sentence = make_sentence(0, "个个都是好样的")
        transcript = {"sentences": [sentence], "dismissed": []}
        changed = pipeline._auto_correct(transcript)
        self.assertEqual(changed, 0)
        self.assertEqual(transcript["sentences"][0]["text"], "个个都是好样的")


# ------------------------------------------- F. ABAB reduplication whitelist


ABAB_WORDS = ("琢磨", "打扮", "高兴", "热闹", "放松", "欣赏", "享受",
              "打听", "运动", "清理", "参观", "表演", "联系", "汇报",
              "演示", "测试", "核对", "记录", "解释", "说明", "确认",
              "核实", "排查", "梳理", "复盘", "洗澡", "散步", "聊天",
              "唱歌", "跳舞")


class AbabWhitelistTests(PipelineCase):
    """F: 琢磨琢磨 / 打扮打扮 ... are grammar, auto-correct must not fold them."""

    def test_whitelist_contains_every_new_abab_word(self):
        for word in ABAB_WORDS:
            self.assertIn(word, analysis.LEGIT_REDUP, word)

    def test_doubled_abab_words_are_not_repeats(self):
        for word in ABAB_WORDS:
            issues = analysis.find_repeats([make_sentence(0, word * 2)])
            self.assertEqual(issues, [], word)

    def test_auto_correct_keeps_a_doubled_abab_word(self):
        pipeline = self.make_pipeline()
        transcript = {"sentences": [make_sentence(0, "你先琢磨琢磨再说")],
                      "dismissed": []}
        self.assertEqual(pipeline._auto_correct(transcript), 0)
        self.assertEqual(transcript["sentences"][0]["text"], "你先琢磨琢磨再说")


# ----------------------------------- G. numeral + classifier reduplication


QUANTIFIER_TEXTS = ("一个一个", "一条一条", "一份一份", "一句一句",
                    "两天两天", "一口一口", "一层一层",
                    # 只在 cleanup._MEASURE_CHARS 里、易被漏掉的量词
                    "一封一封", "一捆一捆", "一代一代", "一克一克")


class QuantifierReduplicationTests(PipelineCase):
    """G: 一个一个 / 一条一条 is distributive grammar, not a stutter."""

    def test_rule_recognises_numeral_plus_classifier(self):
        for block in ("一个", "一条", "一份", "一句", "两天", "几遍", "半碗"):
            self.assertTrue(analysis._is_quantifier_reduplication(block), block)
        for block in ("一定", "一起", "这个", "好像", "研究", "我"):
            self.assertFalse(analysis._is_quantifier_reduplication(block), block)

    def test_quantifier_reduplications_are_not_repeats(self):
        for text in QUANTIFIER_TEXTS:
            issues = analysis.find_repeats([make_sentence(0, text)])
            self.assertEqual(issues, [], text)

    def test_real_repeats_still_report(self):
        for text in ("这个这个", "好像好像", "我我", "一定一定", "一起一起"):
            issues = analysis.find_repeats([make_sentence(0, text)])
            self.assertTrue(issues, text)
            self.assertIn(text, {i["text"] for i in issues}, text)

    def test_auto_correct_keeps_quantifier_reduplication(self):
        pipeline = self.make_pipeline()
        transcript = {"sentences": [make_sentence(0, "我们一个一个来处理")],
                      "dismissed": []}
        self.assertEqual(pipeline._auto_correct(transcript), 0)
        self.assertEqual(transcript["sentences"][0]["text"], "我们一个一个来处理")


# ------------------------------------------- H. pipeline websocket job events


class _EmitStore:
    """Store stub whose get_job can return None (delete raced the event)."""

    def __init__(self, row):
        self.row = row

    def get_job(self, job_id):
        return dict(self.row) if self.row is not None else None

    def update_job(self, job_id, **fields):
        return None


class PipelineEmitTests(PipelineCase):
    """H: emitted job must match server.public_job (URLs, never null)."""

    def make_emit_pipeline(self, row):
        events = []
        pipeline = Pipeline(self.paths, _EmitStore(row), dict(DEFAULT_SETTINGS),
                            RuntimeStub(keep=99), emit=events.append)
        return pipeline, events

    def test_event_carries_thumb_and_media_urls(self):
        row = {"id": "jid", "name": "clip.mp4", "status": "processing",
               "thumb_path": "D:/thumbs/jid.jpg"}
        pipeline, events = self.make_emit_pipeline(row)
        pipeline._report("jid", "asr", 0.5, "识别中", force=True)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["type"], "job")
        self.assertEqual(event["message"], "识别中")
        job = event["job"]
        self.assertEqual(job["thumb_url"], "/api/jobs/jid/thumb")
        self.assertEqual(job["media_url"], "/api/jobs/jid/media")
        self.assertEqual(job["id"], "jid")

    def test_job_without_thumb_has_no_urls(self):
        row = {"id": "jid2", "name": "clip.mp4", "status": "processing"}
        pipeline, events = self.make_emit_pipeline(row)
        pipeline._report("jid2", "probe", 0.1, "读取信息", force=True)
        self.assertEqual(len(events), 1)
        self.assertNotIn("thumb_url", events[0]["job"])
        self.assertNotIn("media_url", events[0]["job"])

    def test_deleted_job_emits_nothing(self):
        pipeline, events = self.make_emit_pipeline(None)
        pipeline._report("gone", "asr", 0.5, "识别中", force=True)
        self.assertEqual(events, [])


# ---------------------------------------- I. <job>.wav.ffmpeg.log cleanup


class FfmpegLogCleanupTests(PipelineCase):
    """I: the ffmpeg diagnostics file never outlives the attempt."""

    def make_cache_files(self, job_id: str):
        cache = self.paths.cache_dir
        cache.mkdir(parents=True, exist_ok=True)
        wav = cache / "{}.wav".format(job_id)
        wav.write_bytes(b"RIFF" + b"\x00" * 60)
        meta = cache / "{}.wav.meta.json".format(job_id)
        meta.write_text("{}", encoding="utf-8")
        log = Pipeline._ffmpeg_log_path(cache, job_id)
        log.write_text("ffmpeg noise", encoding="utf-8")
        return wav, meta, log

    def test_cleanup_removes_log_and_wav(self):
        wav, meta, log = self.make_cache_files("job-clean")
        self.make_pipeline()._cleanup("job-clean")
        self.assertFalse(log.exists())
        self.assertFalse(wav.exists())
        self.assertFalse(meta.exists())

    def test_keep_wav_keeps_wav_but_removes_log(self):
        wav, meta, log = self.make_cache_files("job-keep")
        settings = dict(DEFAULT_SETTINGS, keep_wav=True)
        pipeline = Pipeline(self.paths, self.store, settings, RuntimeStub(keep=99))
        pipeline._cleanup("job-keep")
        self.assertFalse(log.exists())
        self.assertTrue(wav.exists())
        self.assertTrue(meta.exists())

    def test_retry_removes_stale_log_before_working(self):
        job_id = "job-retry"
        wav, meta, log = self.make_cache_files(job_id)
        self.store.rows[job_id] = {"id": job_id, "name": "clip.mp4",
                                   "path": "clip.mp4", "status": "queued"}
        pipeline = self.make_pipeline()
        observed = {}

        def fake_run(job, t0, cancel):
            observed["log_at_start"] = log.exists()
            raise JobError("no_audio", "该文件没有音轨，无法转录")

        pipeline._run = fake_run
        with self.assertRaises(JobError):
            pipeline.run(job_id)
        self.assertFalse(observed["log_at_start"],
                         "run() must clear a stale log before doing work")
        self.assertFalse(log.exists())
        self.assertFalse(wav.exists())
        self.assertEqual(self.store.get_job(job_id)["status"], "error")

    def test_pipeline_log_path_matches_media_naming(self):
        wav = self.paths.cache_dir / "jid.wav"
        expected = wav.with_name(wav.name + ".ffmpeg.log")
        self.assertEqual(Pipeline._ffmpeg_log_path(self.paths.cache_dir, "jid"),
                         expected)


@unittest.skipUnless(os.name == "nt", "uses a .cmd ffmpeg shim")
class ExtractAudioLogCleanupTests(unittest.TestCase):
    """I(b): media.extract_audio deletes its stderr side file on every exit."""

    def setUp(self):
        self.tmp = scratch_dir("media-log")
        self.cache = self.tmp / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.src = self.tmp / "in.mp4"
        self.src.write_bytes(b"not a real video")
        self.dst = self.cache / "job.wav"
        self.log = self.cache / "job.wav.ffmpeg.log"
        self.bin = self.tmp / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.ok_ffmpeg = self.write_shim("ok", "@echo off\r\nexit /b 0\r\n")
        self.fail_ffmpeg = self.write_shim(
            "fail", "@echo off\r\necho boom 1>&2\r\nexit /b 1\r\n")
        self.slow_ffmpeg = self.write_shim(
            "slow", "@echo off\r\nping -n 6 127.0.0.1 >nul\r\nexit /b 0\r\n")

    def tearDown(self):
        remove_scratch(self.tmp)

    def write_shim(self, name: str, body: str) -> Path:
        path = self.bin / "fake_{}.cmd".format(name)
        path.write_text(body, encoding="utf-8")
        return path

    def test_log_removed_on_success(self):
        media.extract_audio(self.ok_ffmpeg, self.src, self.dst, 1000)
        self.assertFalse(self.log.exists())

    def test_log_removed_on_failure(self):
        with self.assertRaises(RuntimeError) as ctx:
            media.extract_audio(self.fail_ffmpeg, self.src, self.dst, 1000)
        self.assertIn("boom", str(ctx.exception))
        self.assertFalse(self.log.exists())

    def test_log_removed_on_cancel(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            media.extract_audio(self.slow_ffmpeg, self.src, self.dst, 1000,
                                cancel=cancel)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
