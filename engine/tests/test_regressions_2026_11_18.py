"""Regression tests for the 2026-11-17 supplementary audit fixes (A2-A6/A10/A11/B5).

Each test pins one defect that the 110-test suite did not cover before the
2026-11-18 repair pass.
"""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from voicetrace_engine import edit, export, media, segmentation, textutil
from voicetrace_engine.config import DEFAULT_SETTINGS, Paths
from voicetrace_engine.pipeline import Pipeline
from voicetrace_engine.runtime import AsrItem, AsrResult
from voicetrace_engine.store import Store


def content_len(text: str) -> int:
    return len(textutil.content_text(text))


def make_sentence(sid: int, text: str, start: int = 0, step_ms: int = 200) -> dict:
    words = []
    for i, ch in enumerate(text):
        if not textutil.is_content(ch):
            continue
        words.append({"text": ch, "start_ms": start + i * step_ms,
                      "end_ms": start + (i + 1) * step_ms})
    return {
        "id": sid, "start_ms": start, "end_ms": start + max(1, len(text)) * step_ms,
        "text": text, "conf": 0.9, "words": words, "edited": False,
    }


class TextBudgetTests(unittest.TestCase):
    def test_single_line_keeps_all_content_characters(self):
        text = "甲乙丙丁戊己庚辛壬癸子，丑寅卯辰巳午未申酉戌亥"
        self.assertEqual(content_len(text), 22)
        out = textutil.wrap_subtitle(text, max_chars=22, max_lines=1)
        self.assertEqual(content_len(out), 22, "A3: single-line export lost a character")
        self.assertIn("亥", out)

    def test_single_line_still_truncates_when_really_too_long(self):
        text = "一二三四五六七八九十一二三四五六七八九十一二三四五"
        out = textutil.wrap_subtitle(text, max_chars=22, max_lines=1)
        self.assertEqual(content_len(out), 22)


class SegmentationRobustnessTests(unittest.TestCase):
    def test_few_characters_over_long_pause_does_not_crash(self):
        sentence = make_sentence(0, "甲乙", start=0, step_ms=10000)
        pieces = segmentation.segment_sentence(sentence, max_chars=22, max_ms=6500)
        self.assertEqual(content_len("".join(p["text"] for p in pieces)), 2)
        for piece in pieces:
            self.assertGreaterEqual(content_len(piece["text"]), 1)


class MediaProgressTests(unittest.TestCase):
    def test_out_time_timestamp_is_parsed(self):
        self.assertAlmostEqual(media._parse_ts("00:01:02.500"), 62.5)
        self.assertAlmostEqual(media._parse_ts("01:00:00.000"), 3600.0)


class EditTimingTests(unittest.TestCase):
    def test_middle_insertion_keeps_positive_durations(self):
        sentence = make_sentence(0, "甲乙", start=0, step_ms=340)
        edit.set_sentence_text(sentence, "甲丙丁戊己乙")
        words = sentence["words"]
        self.assertEqual("".join(w["text"] for w in words), "甲丙丁戊己乙")
        for word in words:
            self.assertGreater(word["end_ms"], word["start_ms"],
                               "A10: zero-duration word after a middle insert")
        starts = [w["start_ms"] for w in words]
        self.assertEqual(starts, sorted(starts))


class ExportMediaEndTests(unittest.TestCase):
    def test_last_cue_never_extends_past_media_end(self):
        transcript = {"sentences": [make_sentence(0, "甲", start=900, step_ms=100)],
                      "media": {"duration_ms": 1000}}
        cues = export.build_cues(transcript, max_chars=22, max_lines=2, settings={})
        self.assertTrue(cues)
        self.assertLessEqual(cues[-1]["end"], 1000)

    def test_cue_starting_after_media_end_is_dropped(self):
        transcript = {"sentences": [make_sentence(0, "甲", start=1500, step_ms=100)],
                      "media": {"duration_ms": 1000}}
        cues = export.build_cues(transcript, max_chars=22, max_lines=2, settings={})
        self.assertEqual(cues, [])


class _ScratchCase(unittest.TestCase):
    def setUp(self):
        import time
        import uuid
        root = Path(__file__).resolve().parents[3] / "_scratch" / time.strftime("%Y-%m-%d") / "engine-tests"
        self.tmp = root / ("reg18-" + uuid.uuid4().hex[:8])
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.paths = Paths(data_dir=self.tmp / "data", models_dir=self.tmp / "models")
        self.paths.ensure()
        for key in ("aed", "vad", "punc"):
            (self.paths.models_dir / key).mkdir(parents=True, exist_ok=True)
        self.store = Store(self.paths.db_path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_pipeline(self, runtime):
        return Pipeline(self.paths, self.store, dict(DEFAULT_SETTINGS), runtime)


class WavCacheTests(_ScratchCase):
    def test_truncated_wav_is_not_reused(self):
        wav = self.paths.cache_dir / "job.wav"
        sf.write(str(wav), np.zeros(16000 * 5, dtype=np.int16), 16000)
        pipeline = self.make_pipeline(SimpleNamespace())
        fingerprint = {"duration_ms": 10000, "size_bytes": 10}
        self.assertFalse(pipeline._wav_reusable(wav, fingerprint),
                         "A2: a 5 s cache must not satisfy a 10 s media")
        pipeline._write_wav_fingerprint(wav, fingerprint)
        self.assertFalse(pipeline._wav_reusable(wav, fingerprint),
                         "A2: duration must still be validated, not just the sidecar")

    def test_complete_wav_with_matching_fingerprint_is_reused(self):
        wav = self.paths.cache_dir / "job2.wav"
        sf.write(str(wav), np.zeros(16000 * 10, dtype=np.int16), 16000)
        pipeline = self.make_pipeline(SimpleNamespace())
        fingerprint = {"duration_ms": 10000, "size_bytes": 10}
        pipeline._write_wav_fingerprint(wav, fingerprint)
        self.assertTrue(pipeline._wav_reusable(wav, fingerprint))


class VadWindowCoverageTests(_ScratchCase):
    def test_continuous_speech_has_no_gap_across_windows(self):
        wav = self.paths.cache_dir / "long.wav"
        sf.write(str(wav), np.zeros(16000 * 40, dtype=np.int16), 16000)
        runtime = SimpleNamespace(detect=lambda audio: [(0, int(len(audio) / 16))])
        pipeline = self.make_pipeline(runtime)
        with sf.SoundFile(str(wav)) as handle:
            segments = pipeline._vad_windows(
                "job", handle, handle.samplerate, handle.frames, 10000,
                threading.Event())
        merged = pipeline._normalize_segments(segments)
        self.assertTrue(merged)
        self.assertEqual(merged[0][0], 0)
        self.assertEqual(merged[-1][1], 40000)
        for prev, nxt in zip(merged, merged[1:]):
            self.assertLessEqual(nxt[0] - prev[1], 200,
                                 "A6: gap at a window boundary: {} -> {}".format(prev, nxt))


class PartialBoundaryTests(_ScratchCase):
    def test_cached_result_with_different_boundary_is_recomputed(self):
        job = self.store.add_job(str(self.tmp / "clip.mp4"), "clip.mp4", 10)
        calls = {"n": 0}

        class Runtime:
            def transcribe(self, items, batch_size=6, progress=None, cancelled=None,
                           on_batch=None):
                calls["n"] += 1
                return [AsrResult(key=it.key, text="fresh", start_ms=it.start_ms,
                                  end_ms=it.end_ms) for it in items]

        pipeline = self.make_pipeline(Runtime())
        item = AsrItem(key="s0", start_ms=5000, end_ms=9000, audio=np.zeros(1600, dtype=np.int16))
        cached = AsrResult(key="s0", text="stale", start_ms=100, end_ms=2000)
        results = pipeline._transcribe(job["id"], [item], {"s0": cached}, {}, threading.Event())
        self.assertEqual(calls["n"], 1, "B5: mismatched boundary must be re-run")
        self.assertEqual(results[0].text, "fresh")

    def test_sidecar_roundtrip_keeps_boundaries(self):
        from voicetrace_engine.pipeline import result_from_json, result_to_json
        row = result_to_json(AsrResult(key="s1", text="x", start_ms=10, end_ms=20))
        self.assertEqual(row["start_ms"], 10)
        self.assertEqual(result_from_json(row).end_ms, 20)


class InitialDbFailureTests(_ScratchCase):
    """B6: a failed first DB write must not leak the cancel event."""

    def test_cancel_event_is_released_when_claim_update_fails(self):
        job = self.store.add_job(str(self.tmp / "clip.mp4"), "clip.mp4", 10)
        pipeline = self.make_pipeline(SimpleNamespace(ready=True))
        original = self.store.update_job
        calls = {"n": 0}

        def flaky(job_id, **fields):
            if calls["n"] == 0:
                calls["n"] += 1
                raise OSError("database is read-only")
            return original(job_id, **fields)

        self.store.update_job = flaky
        with self.assertRaises(OSError):
            pipeline.run(job["id"], threading.Event())
        self.assertEqual(pipeline._cancels, set(),
                         "B6: cancel event leaked after a failed claim")


class RuntimeDeviceTests(_ScratchCase):
    """B7: honour settings.device and roll back a half-loaded model set."""

    def _install_fakes(self, fail_first_aed: bool = False):
        import sys
        import types

        seen = {"vad": [], "punc": [], "asr": [], "aed_failures": 0}

        class FakeVad:
            @staticmethod
            def from_pretrained(path, cfg):
                seen["vad"].append(cfg)
                return object()

        class FakePunc:
            @staticmethod
            def from_pretrained(path, cfg):
                seen["punc"].append(cfg)
                return object()

        class FakeAsr:
            @staticmethod
            def from_pretrained(name, path, cfg):
                seen["asr"].append(cfg)
                if fail_first_aed and seen["aed_failures"] == 0:
                    seen["aed_failures"] += 1
                    raise RuntimeError("AED load failed")
                return object()

        def cfg(**kwargs):
            return kwargs

        package = types.ModuleType("fireredasr2s")
        package.__path__ = []
        vad_mod = types.ModuleType("fireredasr2s.fireredvad")
        vad_mod.FireRedVad = FakeVad
        vad_mod.FireRedVadConfig = cfg
        punc_mod = types.ModuleType("fireredasr2s.fireredpunc")
        punc_mod.FireRedPunc = FakePunc
        punc_mod.FireRedPuncConfig = cfg
        asr_mod = types.ModuleType("fireredasr2s.fireredasr2")
        asr_mod.FireRedAsr2 = FakeAsr
        asr_mod.FireRedAsr2Config = cfg
        modules = {
            "fireredasr2s": package,
            "fireredasr2s.fireredvad": vad_mod,
            "fireredasr2s.fireredpunc": punc_mod,
            "fireredasr2s.fireredasr2": asr_mod,
        }
        saved = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)

        class FakeCuda:
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def set_per_process_memory_fraction(_fraction):
                return None

            @staticmethod
            def empty_cache():
                return None

        class FakeTorch:
            cuda = FakeCuda()

        def fake_import(self):
            self._torch = FakeTorch()
            self._loaded_modules = True

        def restore():
            for name, old in saved.items():
                if old is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old

        return seen, fake_import, restore

    def _runtime(self):
        from voicetrace_engine.runtime import Runtime
        return Runtime(self.paths, dict(DEFAULT_SETTINGS), use_gpu=True)

    def test_cpu_setting_is_honoured_even_when_cuda_exists(self):
        seen, fake_import, restore = self._install_fakes()
        try:
            runtime = self._runtime()
            runtime.settings["device"] = "cpu"
            runtime._import = fake_import.__get__(runtime, type(runtime))
            runtime.load()
            self.assertEqual(runtime.device, "cpu")
            self.assertFalse(seen["asr"][0]["use_gpu"])
            self.assertFalse(seen["punc"][0]["use_gpu"])
        finally:
            restore()

    def test_failed_model_load_rolls_back_partial_instances(self):
        seen, fake_import, restore = self._install_fakes(fail_first_aed=True)
        try:
            runtime = self._runtime()
            runtime.settings["device"] = "cuda"
            runtime._import = fake_import.__get__(runtime, type(runtime))
            with self.assertRaises(RuntimeError):
                runtime.load()
            self.assertFalse(runtime.ready)
            self.assertIsNone(runtime._vad)
            self.assertIsNone(runtime._punc)
            self.assertIsNone(runtime._asr)
            # The retry must observe no stale factory instance and reach ready.
            runtime.load()
            self.assertTrue(runtime.ready)
            self.assertEqual(len(seen["asr"]), 2)
        finally:
            restore()


if __name__ == "__main__":
    unittest.main()
