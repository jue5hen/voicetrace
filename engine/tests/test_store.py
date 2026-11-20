"""Store / transcript-file tests for the engine.

The SQLite store is the only mutable engine state, so these tests pin the job
lifecycle, the undo history cap, the WAL checkpoint used by the graceful
shutdown, and the transcript file round-trip. Everything is created in a
throw-away database under <project>/_scratch/<YYYY-MM-DD>/.
"""
from __future__ import annotations

import json
import shutil
import time
import unittest
import uuid
from pathlib import Path

from voicetrace_engine import server as engine_server
from voicetrace_engine.segmentation import SEG_VERSION
from voicetrace_engine.store import Store

PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ helpers


def scratch_dir(tag: str) -> Path:
    base = PROJECT_ROOT / "_scratch" / time.strftime("%Y-%m-%d") / "engine-tests"
    path = base / "{}-{}".format(tag, uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_scratch(path: Path) -> None:
    target = Path(path).resolve()
    if PROJECT_ROOT not in target.parents or "_scratch" not in target.parts:
        return
    shutil.rmtree(target, ignore_errors=True)
    try:
        target.parent.rmdir()  # only removes the shared parent when empty
    except OSError:
        pass


def make_env(root: Path):
    """Fake data/models/ffmpeg layout (State() must not need a toolchain)."""
    data = root / "data"
    models = root / "models"
    bin_dir = root / "bin"
    for key in ("aed", "vad", "punc"):
        (models / key).mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    for exe in ("ffmpeg", "ffprobe"):
        (bin_dir / (exe + ".exe")).write_bytes(b"stub")
    return data, models, bin_dir


def make_sentence(sid, text, start_ms=1000):
    return {
        "id": sid,
        "start_ms": start_ms,
        "end_ms": start_ms + max(1, len(text)) * 120,
        "text": text,
        "conf": 0.9,
        "words": [{"text": ch, "start_ms": start_ms + i * 120,
                   "end_ms": start_ms + (i + 1) * 120}
                  for i, ch in enumerate(text) if ch not in "，。！？"],
        "edited": False,
    }


# ------------------------------------------------------------------- tests


class StoreJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch_dir("store-jobs")
        self.store = Store(self.tmp / "voicetrace.db")

    def tearDown(self):
        try:
            self.store.close()
        finally:
            remove_scratch(self.tmp)

    def add(self, name="clip.mp4", **kwargs):
        return self.store.add_job("D:/videos/" + name, name, 4321, **kwargs)

    def test_add_get_list_delete(self):
        job = self.add(duration_ms=5000, width=1920, height=1080)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["stage"], "queued")
        self.assertFalse(job["has_transcript"])
        # 新任务默认「全部自动校正」
        self.assertTrue(job["auto_correct"])

        fetched = self.store.get_job(job["id"])
        self.assertEqual(fetched["path"], "D:/videos/clip.mp4")
        self.assertEqual(fetched["duration_ms"], 5000)
        self.assertEqual((fetched["width"], fetched["height"]), (1920, 1080))

        self.assertEqual([j["id"] for j in self.store.list_jobs()], [job["id"]])

        self.store.delete_job(job["id"])
        self.assertIsNone(self.store.get_job(job["id"]))
        self.assertEqual(self.store.list_jobs(), [])

    def test_auto_correct_defaults_on_but_can_be_disabled(self):
        self.assertTrue(self.add()["auto_correct"])
        off = self.add("clip2.mp4", auto_correct=False)
        self.assertFalse(off["auto_correct"])

    def test_update_job_copies_known_fields_only(self):
        job = self.add()
        self.store.update_job(job["id"], status="done", stage="done", progress=1.0,
                              has_transcript=1, bogus_field="ignored")
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["stage"], "done")
        self.assertAlmostEqual(row["progress"], 1.0)
        self.assertTrue(row["has_transcript"])
        self.assertNotIn("bogus_field", row)
        # unknown job ids must not explode
        self.store.update_job("missing", status="done")

    def test_issues_count_is_derived(self):
        job = self.add()
        self.store.update_job(job["id"], issues_json=json.dumps({"total": 7}))
        self.assertEqual(self.store.get_job(job["id"])["issues_count"], 7)

    def test_next_queued_and_pending_count(self):
        first = self.add("1.mp4")
        second = self.add("2.mp4")
        self.assertEqual(self.store.pending_count(), 2)
        self.assertIsNotNone(self.store.next_queued())

        self.store.update_job(first["id"], status="done", stage="done")
        self.assertEqual(self.store.pending_count(), 1)
        self.assertEqual(self.store.next_queued()["id"], second["id"])
        self.store.update_job(second["id"], status="processing")
        self.assertEqual(self.store.pending_count(), 1)
        self.store.update_job(second["id"], status="error")
        self.assertEqual(self.store.pending_count(), 0)
        self.assertIsNone(self.store.next_queued())

    def test_reset_stale_requeues_processing_jobs(self):
        job = self.add()
        self.store.update_job(job["id"], status="processing", stage="asr",
                              progress=0.5, error="boom")
        self.assertEqual(self.store.reset_stale(), 1)
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["stage"], "queued")
        self.assertEqual(row["progress"], 0)
        self.assertIsNone(row["error"])
        # nothing left to reset
        self.assertEqual(self.store.reset_stale(), 0)

    def test_history_is_lifo(self):
        job = self.add()
        for version in range(5):
            self.store.push_history(job["id"], json.dumps({"version": version}), "edit")
        self.assertEqual(self.store.history_count(job["id"]), 5)
        self.assertEqual(json.loads(self.store.pop_history(job["id"]))["version"], 4)
        self.assertEqual(json.loads(self.store.pop_history(job["id"]))["version"], 3)
        self.assertIsNone(self.store.pop_history("missing-job"))

    def test_history_is_capped(self):
        job = self.add()
        for version in range(20):
            self.store.push_history(job["id"], json.dumps({"version": version}),
                                    "edit", keep=4)
        self.assertEqual(self.store.history_count(job["id"]), 4)
        self.assertEqual(json.loads(self.store.pop_history(job["id"]))["version"], 19)

    def test_delete_job_drops_history(self):
        job = self.add()
        self.store.push_history(job["id"], json.dumps({"version": 1}), "edit")
        self.store.delete_job(job["id"])
        self.assertEqual(self.store.history_count(job["id"]), 0)

    def test_wal_checkpoint_is_callable(self):
        job = self.add()
        self.store.push_history(job["id"], json.dumps({"version": 1}), "edit")
        result = self.store.checkpoint()
        self.assertIsInstance(result, tuple)
        self.assertIsNotNone(self.store.get_job(job["id"]))
        # the db keeps working after the checkpoint
        self.store.update_job(job["id"], status="done")
        self.assertEqual(self.store.get_job(job["id"])["status"], "done")

    def test_close_is_idempotent(self):
        self.add()
        self.store.close()
        self.store.close()


class TranscriptFileTests(unittest.TestCase):
    """Transcript save/read through the same State the HTTP layer uses."""

    def setUp(self):
        self.tmp = scratch_dir("store-transcript")
        data, models, bin_dir = make_env(self.tmp)
        self.state = engine_server.State(engine_server.parse_args([
            "--token", "store-token",
            "--data-dir", str(data),
            "--models-dir", str(models),
            "--ffmpeg", str(bin_dir),
        ]))

    def tearDown(self):
        try:
            self.state.store.close()
        finally:
            remove_scratch(self.tmp)

    def test_save_then_load(self):
        job = self.state.store.add_job("D:/videos/x.mp4", "x.mp4", 10)
        transcript = {
            "job_id": job["id"],
            "version": 3,
            "seg_version": SEG_VERSION,
            "media": {"name": "x.mp4", "path": "D:/videos/x.mp4", "duration_ms": 2000},
            "sentences": [make_sentence(0, "第一句。"), make_sentence(1, "第二句。", 3000)],
            "dismissed": [],
            "stats": {},
        }
        self.state.save_transcript(job["id"], transcript)

        path = self.state.paths.transcript_dir / (job["id"] + ".json")
        self.assertTrue(path.is_file())
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["sentences"][0]["text"], "第一句。")

        loaded = self.state.load_transcript(job["id"])
        self.assertEqual(loaded["version"], 3)
        self.assertEqual([s["text"] for s in loaded["sentences"]],
                         ["第一句。", "第二句。"])
        row = self.state.store.get_job(job["id"])
        self.assertEqual(Path(row["transcript_path"]), path)
        self.assertTrue(row["has_transcript"])

    def test_missing_transcript_is_404(self):
        job = self.state.store.add_job("D:/videos/y.mp4", "y.mp4", 10)
        with self.assertRaises(engine_server.HTTPException) as caught:
            self.state.load_transcript(job["id"])
        self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(engine_server.HTTPException) as caught:
            self.state.load_transcript("does-not-exist")
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
