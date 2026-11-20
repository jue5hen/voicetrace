"""Regression tests for the 2026-11-18 engine API / store-layer audit.

Scope: server.py, config.py, store.py, edit.py.  Every test pins one defect
that was reproduced from the source before the fix; the docstring of each test
states the old (buggy) behaviour so the test is a real guard, not a snapshot.

No model, no GPU, no real ffmpeg: the engine runs on stub binaries and a
throw-away data directory under <project>/_scratch/<date>/engine-api/.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import uvicorn

from voicetrace_engine import config as engine_config
from voicetrace_engine import edit, segmentation, textutil
from voicetrace_engine import server as engine_server
from voicetrace_engine.config import DEFAULT_SETTINGS
from voicetrace_engine.media import MediaInfo
from voicetrace_engine.segmentation import SEG_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ helpers


def scratch_dir(tag: str) -> Path:
    path = (PROJECT_ROOT / "_scratch" / time.strftime("%Y-%m-%d") / "engine-api"
            / "{}-{}".format(tag, uuid.uuid4().hex[:8]))
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_scratch(path: Path) -> None:
    """Only ever delete a directory this suite created under _scratch/engine-api."""
    target = Path(path).resolve()
    if PROJECT_ROOT not in target.parents or "engine-api" not in target.parts:
        return
    shutil.rmtree(target, ignore_errors=True)
    try:
        target.parent.rmdir()  # shared parent, only removed when empty
    except OSError:
        pass


def make_env(root: Path):
    """Fake data/models/ffmpeg layout so State() never needs a real toolchain."""
    data = root / "data"
    models = root / "models"
    bin_dir = root / "bin"
    for key in ("aed", "vad", "punc"):
        (models / key).mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    for exe in ("ffmpeg", "ffprobe"):
        (bin_dir / (exe + ".exe")).write_bytes(b"stub")
    return data, models, bin_dir


def make_sentence(sid: int, text: str, start_ms: int = 1000) -> dict:
    words = [{"text": ch, "start_ms": start_ms + i * 120,
              "end_ms": start_ms + (i + 1) * 120}
             for i, ch in enumerate(text) if ch not in "，。！？、,.!?"]
    return {
        "id": sid,
        "start_ms": start_ms,
        "end_ms": start_ms + max(1, len(text)) * 120,
        "text": text,
        "conf": 0.9,
        "words": words,
        "edited": False,
    }


def make_transcript(texts: list[str]) -> dict:
    sentences = [make_sentence(i, text, 1000 + i * 2000)
                 for i, text in enumerate(texts)]
    return {
        "job_id": "stub",
        "version": 1,
        "seg_version": SEG_VERSION,
        "media": {"name": "clip.mp4", "path": "clip.mp4",
                  "duration_ms": len(texts) * 2000, "width": 1280, "height": 720},
        "sentences": sentences,
        "dismissed": [],
        "stats": {},
    }


def parse_state_args(tmp: Path, token: str):
    data, models, bin_dir = make_env(tmp)
    return engine_server.parse_args([
        "--token", token,
        "--data-dir", str(data),
        "--models-dir", str(models),
        "--ffmpeg", str(bin_dir),
    ])


# ------------------------------------------------------------------- fixture


class EngineApiTest(unittest.TestCase):
    """One uvicorn server + one data dir per class, driven with http.client."""

    token = "audit-2026-11-18-token"

    @classmethod
    def setUpClass(cls):
        cls.tmp = scratch_dir("api")
        cls.state = engine_server.State(parse_state_args(cls.tmp, cls.token))
        cls._real_probe_gpu = engine_server.probe_gpu
        engine_server.probe_gpu = lambda: {"cuda": False, "device": "cpu",
                                           "name": None, "vram_mb": None}
        app = engine_server.build_app(cls.state)

        cls.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(64)
        cls.port = cls.sock.getsockname()[1]
        config = uvicorn.Config(app, log_config=None, access_log=False,
                                log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run,
                                      kwargs={"sockets": [cls.sock]},
                                      name="vt-audit-api", daemon=True)
        cls.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline and not cls.server.started:
            if not cls.thread.is_alive():
                break
            time.sleep(0.02)
        if not cls.server.started:
            raise RuntimeError("uvicorn did not start for the audit API tests")

    @classmethod
    def tearDownClass(cls):
        engine_server.probe_gpu = cls._real_probe_gpu
        try:
            cls.server.should_exit = True
            cls.thread.join(timeout=15)
        finally:
            try:
                cls.sock.close()
            except OSError:
                pass
            try:
                cls.state.store.close()
            except Exception:  # noqa: BLE001
                pass
            remove_scratch(cls.tmp)

    # -- helpers -----------------------------------------------------
    def request(self, method, path, body=None, headers=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        payload = None
        hdrs = dict(headers or {})
        hdrs["Authorization"] = "Bearer " + self.token
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
            ctype = response.getheader("Content-Type") or ""
        finally:
            conn.close()
        data = None
        if raw and "json" in ctype:
            data = json.loads(raw.decode("utf-8"))
        return status, data, raw

    def make_job(self, texts=("第一句测试文本。",)) -> str:
        src = self.tmp / "media" / "clip-{}.mp4".format(uuid.uuid4().hex[:8])
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"stub-media")
        job = self.state.store.add_job(str(src), src.name, src.stat().st_size)
        transcript = make_transcript(list(texts))
        transcript["job_id"] = job["id"]
        transcript["media"]["name"] = src.name
        transcript["media"]["path"] = str(src)
        self.state.save_transcript(job["id"], transcript)
        return job["id"]


# ------------------------------------------------ F1: shared transcript lock


class TranscriptLockSharingTest(EngineApiTest):
    """B1-still-broken: the pipeline never got the editor's transcript lock.

    State built transcript_lock *after* constructing Pipeline and did not pass
    it, so pipeline._save_transcript used a private RLock.  The worker's final
    save and a PUT/apply/undo could then both read version N and both write
    N+1 for different content (a lost edit, no CAS conflict).
    """

    def test_pipeline_uses_the_editor_transcript_lock(self):
        self.assertIs(self.state.pipeline._transcript_lock,
                      self.state.transcript_lock,
                      "pipeline._save_transcript must share the editor lock")

    def test_pipeline_save_waits_for_an_in_flight_edit(self):
        job_id = self.make_job()
        transcript = json.loads(
            self.state.transcript_path(job_id).read_text(encoding="utf-8"))
        transcript["sentences"][0]["text"] = "流水线写入的字幕。"
        finished = threading.Event()
        errors = []

        def save():
            try:
                self.state.pipeline._save_transcript(job_id, transcript)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                finished.set()

        self.state.transcript_lock.acquire()
        try:
            worker = threading.Thread(target=save, name="vt-pipeline-save",
                                      daemon=True)
            worker.start()
            self.assertFalse(
                finished.wait(0.3),
                "pipeline save must block while an edit holds the lock")
        finally:
            self.state.transcript_lock.release()
        self.assertTrue(finished.wait(10))
        worker.join(5)
        self.assertEqual(errors, [])
        self.assertIn("流水线写入的字幕",
                      self.state.transcript_path(job_id).read_text(encoding="utf-8"))


# ------------------------------------------------ F2: worker must not die


class WorkerResilienceTest(unittest.TestCase):
    """A transient sqlite error used to kill the only worker thread.

    Worker.run called next_queued() / get_job() / queue_info() outside any
    try/except, so one OperationalError (locked db, full disk) ended the
    thread: every later job silently stayed "queued" until a restart.
    """

    def setUp(self):
        self.tmp = scratch_dir("worker")
        self.state = engine_server.State(
            parse_state_args(self.tmp, "worker-resilience-token"))

    def tearDown(self):
        try:
            self.state.worker.stop.set()
            self.state.worker.join(10)
        finally:
            try:
                self.state.store.close()
            except Exception:  # noqa: BLE001
                pass
            remove_scratch(self.tmp)

    def test_transient_queue_error_does_not_kill_the_worker(self):
        store = self.state.store
        real_next = store.next_queued
        calls = {"n": 0, "raised": 0}

        def flaky():
            calls["n"] += 1
            if calls["raised"] == 0:
                calls["raised"] += 1
                raise sqlite3.OperationalError("database is locked")
            return real_next()

        worker = self.state.worker
        store.next_queued = flaky
        try:
            # The loop logs the injected OperationalError with log.exception;
            # swallow that one traceback so a green run stays readable.
            with mock.patch.object(engine_server.log, "exception"):
                worker.start()
                deadline = time.time() + 10
                while time.time() < deadline and calls["n"] < 2:
                    time.sleep(0.05)
            self.assertGreaterEqual(
                calls["n"], 2,
                "worker thread died after one transient sqlite error")
            self.assertTrue(worker.is_alive(),
                            "worker thread died after one transient sqlite error")
        finally:
            worker.stop.set()
            worker.join(10)
            del store.next_queued


# ------------------------------------------- F4: export fallback overwrite


class ExportFallbackTest(EngineApiTest):
    """The fallback export target used to silently overwrite an old export.

    SPEC 3: same-name default exports append " (2)", " (3)"; only an explicit
    path is treated as the user confirming an overwrite.  When the explicitly
    requested path could not be written, the handler retried at
    data/exports/<same name> with os.replace and clobbered whatever was there.
    """

    def test_fallback_never_overwrites_an_existing_export(self):
        job_id = self.make_job()
        exports = self.state.paths.data_dir / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        sentinel = exports / "clip.srt"
        sentinel.write_text("旧的导出内容", encoding="utf-8")
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        requested = blocker / "clip.srt"  # parent is a file -> write must fail

        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/export".format(job_id),
            {"format": "srt", "path": str(requested)})

        self.assertEqual(status, 200, data)
        self.assertTrue(data["fallback"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "旧的导出内容",
                         "fallback export overwrote an earlier file")
        created = sorted(p.name for p in exports.glob("clip*.srt"))
        self.assertEqual(created, ["clip (2).srt", "clip.srt"],
                         "fallback must append a sequence number")
        self.assertIn("-->", (exports / "clip (2).srt").read_text(encoding="utf-8-sig"))


# --------------------------------------------- F5: import stat race -> 500


class ImportRobustnessTest(EngineApiTest):
    """One vanished file used to 500 the whole import batch.

    create_jobs probed the file and then called p.stat().st_size with no
    OSError guard; a file deleted (or locked) between those two steps raised
    out of the handler, so the request answered 500 and no job was created.
    """

    def test_file_vanishing_during_probe_is_reported_as_skipped(self):
        src = self.tmp / "media" / "vanish.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"stub-media")

        def probe_then_vanish(_ffprobe, path):
            Path(path).unlink()
            return MediaInfo(duration_ms=1000, width=640, height=360, has_audio=True)

        with mock.patch.object(engine_server, "probe", probe_then_vanish):
            status, data, _raw = self.request(
                "POST", "/api/jobs", {"paths": [str(src)]})

        self.assertEqual(status, 200, data)
        self.assertEqual(data["jobs"], [])
        self.assertEqual(len(data["skipped"]), 1)
        self.assertEqual(data["skipped"][0]["reason"], "文件不可读")


# ---------------------------------------- F8/F9: malformed apply actions


class ApplyActionRobustnessTest(EngineApiTest):
    """/apply used to answer 500 (and keep a dirty history row) on bad input.

    * {"type": "repeat", "sentence_id": null} raised TypeError out of
      edit.apply_actions (only KeyError/ValueError were caught) -> 500.
    * an unknown scope silently degraded to "all": a typo replaced the whole
      transcript instead of one selected sentence.
    """

    def test_malformed_action_is_not_a_500(self):
        job_id = self.make_job(("第一句测试文本。", "第二句测试文本。"))
        _status, before, _raw = self.request(
            "GET", "/api/jobs/{}/transcript".format(job_id))

        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/apply".format(job_id),
            {"actions": [{"type": "repeat", "sentence_id": None,
                          "start": 0, "end": 2}]})

        # The per-action report is not part of the SPEC response shape, so the
        # contract here is only "a bad action must not turn into a 500".
        self.assertEqual(status, 200, data)
        self.assertEqual([s["text"] for s in data["transcript"]["sentences"]],
                         [s["text"] for s in before["sentences"]])

    def test_unknown_scope_does_not_replace_the_whole_transcript(self):
        job_id = self.make_job(("圣兵来了。", "圣兵走了。"))

        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/apply".format(job_id),
            {"actions": [{"type": "replace", "from": "圣兵", "to": "胜兵",
                          "scope": "selcted", "sentence_ids": [0]}]})

        self.assertEqual(status, 200, data)
        self.assertEqual([s["text"] for s in data["transcript"]["sentences"]],
                         ["圣兵来了。", "圣兵走了。"],
                         "a typo in scope must not become a global replace")


# ------------------------------------------------ F3/F11: delete job files


class DeleteJobFilesTest(EngineApiTest):
    """DELETE /jobs left an orphan transcript / wav fingerprint behind.

    * the file list omitted <id>.wav.meta.json (keep_wav runs and crashed runs
      leave it behind);
    * the unlinks ran without the transcript lock, so an edit that had already
      loaded the transcript saved it back after the delete -> an orphan
      <id>.json with no DB row.
    """

    def test_delete_removes_the_cached_wav_fingerprint(self):
        job_id = self.make_job()
        wav = self.state.paths.cache_dir / "{}.wav".format(job_id)
        meta = self.state.paths.cache_dir / "{}.wav.meta.json".format(job_id)
        wav.write_bytes(b"RIFF" + b"\x00" * 60)
        meta.write_text("{}", encoding="utf-8")

        status, data, _raw = self.request(
            "DELETE", "/api/jobs/{}".format(job_id))

        self.assertEqual(status, 200, data)
        self.assertFalse(wav.exists())
        self.assertFalse(meta.exists(), "wav fingerprint sidecar survived delete")

    def test_delete_waits_for_an_in_flight_edit(self):
        job_id = self.make_job()
        entered = threading.Event()
        release = threading.Event()
        real_save = self.state.save_transcript
        replies = {}

        def slow_save(transcript_id, transcript):
            entered.set()
            release.wait(15)
            return real_save(transcript_id, transcript)

        def do_put():
            replies["put"] = self.request(
                "PUT", "/api/jobs/{}/sentences/0".format(job_id),
                {"text": "编辑中的字幕。"})

        def do_delete():
            replies["delete"] = self.request(
                "DELETE", "/api/jobs/{}".format(job_id))

        with mock.patch.object(self.state, "save_transcript", slow_save):
            put = threading.Thread(target=do_put, name="vt-put", daemon=True)
            put.start()
            self.assertTrue(entered.wait(10), "PUT never reached the save step")
            dele = threading.Thread(target=do_delete, name="vt-delete",
                                    daemon=True)
            dele.start()
            time.sleep(0.4)
            self.assertTrue(dele.is_alive(),
                            "DELETE must wait for the in-flight edit")
            release.set()
            put.join(15)
            dele.join(15)

        self.assertEqual(replies["put"][0], 200, replies.get("put"))
        self.assertEqual(replies["delete"][0], 200, replies.get("delete"))
        self.assertFalse(self.state.transcript_path(job_id).is_file(),
                         "the edit wrote the deleted transcript back")
        self.assertIsNone(self.state.store.get_job(job_id))


# ------------------------------------------ F10: settings publication window


class SettingsPublicationTest(EngineApiTest):
    """PUT /settings used to clear() the live dict before update().

    The same dict object is read by the worker / exporter / segmentation while
    a job runs, so a reader in that window saw a dict without keys:
    settings.get("auto_export") -> None skipped auto export,
    _asr_signature() -> None discarded a resume sidecar.
    """

    class _Watch(dict):
        def __init__(self, data):
            super().__init__(data)
            self.emptied = 0

        def clear(self):
            dict.clear(self)
            if not self:
                self.emptied += 1

    def test_live_settings_are_never_emptied_by_a_save(self):
        watch = self._Watch(self.state.settings)
        original = self.state.settings
        batch = original.get("batch_size")
        self.state.settings = watch
        self.state.runtime.settings = watch
        self.state.pipeline.settings = watch
        try:
            status, data, _raw = self.request(
                "PUT", "/api/settings", {"batch_size": 5})
            self.assertEqual(status, 200, data)
            self.assertEqual(watch["batch_size"], 5)
            self.assertEqual(set(watch), set(DEFAULT_SETTINGS))
            self.assertEqual(watch.emptied, 0,
                             "saving settings emptied the live configuration")
        finally:
            self.state.settings = original
            self.state.runtime.settings = original
            self.state.pipeline.settings = original
            self.request("PUT", "/api/settings", {"batch_size": batch or 6})


# ------------------------------------------------ F6/F7: config module


class ConfigCoercionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch_dir("config")

    def tearDown(self):
        remove_scratch(self.tmp)

    def test_null_string_setting_falls_back_to_the_default(self):
        """A JSON null became the literal string "None" (a junk export dir)."""
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({"export_dir": None, "device": None}),
                        encoding="utf-8")
        loaded = engine_config.load_settings(path)
        self.assertEqual(loaded["export_dir"], "")
        self.assertEqual(loaded["device"], "auto")

        saved = engine_config.save_settings(
            path, {"export_dir": None, "subtitle_max_chars": 30})
        self.assertEqual(saved["export_dir"], "")
        self.assertEqual(saved["subtitle_max_chars"], 30)

        saved = engine_config.save_settings(path, {"export_dir": "D:/out"})
        self.assertEqual(saved["export_dir"], "D:/out")

    def test_engine_root_flag_is_honoured(self):
        """--engine-root was parsed and then ignored by resolve_paths."""
        root = self.tmp / "engine-root"
        (root / "models").mkdir(parents=True, exist_ok=True)
        args = engine_server.parse_args([
            "--engine-root", str(root),
            "--data-dir", str(self.tmp / "data"),
        ])
        paths = engine_config.resolve_paths(args)
        self.assertEqual(paths.engine_root, root.resolve())
        self.assertEqual(paths.models_dir, (root / "models").resolve())

        default_args = engine_server.parse_args(
            ["--data-dir", str(self.tmp / "data-default")])
        default_paths = engine_config.resolve_paths(default_args)
        package_root = Path(engine_config.__file__).resolve().parent.parent
        self.assertEqual(default_paths.engine_root, package_root)
        self.assertEqual(default_paths.models_dir, package_root / "models")


# =====================================================================
# Round 2 (task-4): stats.chars contract, WS snapshot window, /media race,
# relative export_dir, corrupt transcript, ffmpeg side log, OOM requeue lock,
# English word rebuild.
# =====================================================================


# ------------------------------------------- R2: stats.chars = content chars


class StatsCharsTest(EngineApiTest):
    """SPEC 3.2: stats.chars counts content characters, never punctuation.

    All four writers (pipeline._run, segmentation.segment_transcript,
    server._restat, edit.recompute_stats) used to sum len(text), so a
    punctuated transcript reported more characters than the editor/export
    paths and than the desktop client's contentLen.
    """

    def test_segmenter_editor_and_server_agree_on_content_chars(self):
        first = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥又是一段很长的句子没有标点结尾"
        second = "他说：这是第二句，带标点，也要按内容字计算。"
        transcript = {
            "job_id": "t",
            "version": 1,
            "seg_version": 0,
            "media": {"duration_ms": 30000},
            "sentences": [make_sentence(0, first, 0), make_sentence(1, second, 8000)],
            "stats": {},
        }
        self.assertTrue(
            segmentation.segment_transcript(transcript, max_chars=14, force=True))
        joined = "".join(s["text"] for s in transcript["sentences"])
        expected = textutil.count_chars(joined)
        self.assertLess(expected, len(joined),
                        "fixture must contain punctuation for this test to bite")
        self.assertEqual(transcript["stats"]["chars"], expected,
                         "segment_transcript must count content characters")

        self.assertEqual(edit.recompute_stats(transcript)["chars"], expected,
                         "edit.recompute_stats must count content characters")
        engine_server._restat(transcript)
        self.assertEqual(transcript["stats"]["chars"], expected,
                         "server._restat must count content characters")

    def test_edit_endpoint_reports_content_chars(self):
        job_id = self.make_job()
        status, data, _raw = self.request(
            "PUT", "/api/jobs/{}/sentences/0".format(job_id),
            {"text": "你好，世界！"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["transcript"]["stats"]["chars"], 4,
                         "punctuation must not be counted in stats.chars")
        self.assertEqual(data["transcript"]["sentences"][0]["text"], "你好，世界！")


# ------------------------------------------------ R2: WS snapshot window


class _FakeWebSocket:
    """Minimal WebSocket double: records frames and publishes during accept()."""

    def __init__(self, hub, event):
        self.hub = hub
        self.event = event
        self.sent = []
        self.accepted = False

    async def accept(self):
        self.accepted = True
        # An event published while the handshake completes must still reach
        # this client: either it is inside the snapshot, or the fan-out has
        # already registered the socket by then.
        self.hub.publish(self.event)

    async def send_json(self, payload):
        await asyncio.sleep(0)  # let queued fan-out tasks run first
        self.sent.append(payload)


class WsSnapshotWindowTest(unittest.TestCase):
    """An event published during connect() used to be dropped for ever.

    Hub.connect sent the snapshot and only then added the socket to the fan-out
    set, so anything published in between (a job starting/finishing) was lost
    and the client kept stale state until the next event.
    """

    def test_event_published_during_the_handshake_is_delivered(self):
        hub = engine_server.Hub()
        toast = {"type": "toast", "level": "info", "message": "handshake"}

        async def scenario():
            hub.loop = asyncio.get_running_loop()
            socket = _FakeWebSocket(hub, toast)
            await hub.connect(socket, lambda: {"type": "snapshot", "jobs": []})
            for _ in range(20):
                await asyncio.sleep(0)
            return socket

        socket = asyncio.run(scenario())
        kinds = [frame.get("type") for frame in socket.sent
                 if isinstance(frame, dict)]
        self.assertIn("snapshot", kinds)
        self.assertIn("toast", kinds,
                      "event published during the handshake was lost")

    def test_snapshot_is_built_after_the_client_joins_the_fan_out(self):
        hub = engine_server.Hub()
        seen = {"clients": None}

        async def scenario():
            hub.loop = asyncio.get_running_loop()
            socket = _FakeWebSocket(
                hub, {"type": "toast", "level": "info", "message": "x"})

            def build():
                with hub._lock:
                    seen["clients"] = len(hub.clients)
                return {"type": "snapshot", "jobs": []}

            await hub.connect(socket, build)

        asyncio.run(scenario())
        self.assertEqual(seen["clients"], 1,
                         "the snapshot must be built after clients.add")


# -------------------------------------------------- R2: /media stat race


class MediaStatRaceTest(EngineApiTest):
    """/media answered 500 when the video vanished between is_file and stat.

    The route checked path.is_file() and then called path.stat(); a file
    deleted in between made the OSError escape the handler instead of the
    documented 404.
    """

    def test_vanished_media_is_404_not_500(self):
        job_id = self.make_job()
        src = Path(self.state.store.get_job(job_id)["path"])
        real_is_file = Path.is_file
        real_stat = Path.stat

        def fake_is_file(self):
            if self == src:
                return True  # the check passed a moment before the delete
            return real_is_file(self)

        def fake_stat(self, *args, **kwargs):
            if self == src:
                raise FileNotFoundError(2, "vanished")
            return real_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "is_file", fake_is_file), \
                mock.patch.object(Path, "stat", fake_stat):
            status, data, _raw = self.request(
                "GET", "/api/jobs/{}/media".format(job_id))

        self.assertEqual(status, 404, data)
        self.assertEqual(data["error"]["code"], "http_404")


# ------------------------------------------- R2: relative export_dir


class RelativeExportDirTest(EngineApiTest):
    """A relative export_dir was written under the engine CWD and echoed back."""

    def test_relative_export_dir_is_resolved_to_absolute(self):
        job_id = self.make_job()
        target = scratch_dir("relative-export")
        self.addCleanup(remove_scratch, target)
        relative = os.path.relpath(str(target), str(Path.cwd()))
        self.assertFalse(Path(relative).is_absolute())
        previous = self.state.settings.get("export_dir")
        self.state.settings["export_dir"] = relative
        try:
            status, data, _raw = self.request(
                "POST", "/api/jobs/{}/export".format(job_id), {"format": "txt"})
        finally:
            self.state.settings["export_dir"] = previous
        self.assertEqual(status, 200, data)
        written = Path(data["path"])
        self.assertTrue(written.is_absolute(), data["path"])
        self.assertEqual(written.resolve().parent, target.resolve())
        self.assertTrue(written.is_file())


# ------------------------------------------- R2: corrupt transcript file


class CorruptTranscriptTest(EngineApiTest):
    """A damaged transcript file 500'd on every request, with no backup."""

    def test_corrupt_transcript_is_backed_up_then_404(self):
        job_id = self.make_job()
        path = self.state.transcript_path(job_id)
        path.write_text("{ this is not json", encoding="utf-8")

        status, data, _raw = self.request(
            "GET", "/api/jobs/{}/transcript".format(job_id))
        self.assertEqual(status, 500, data)
        self.assertEqual(data["error"]["code"], "http_500")
        self.assertIn("损坏", data["error"]["message"])

        backup = path.with_name(path.name + ".corrupt")
        self.assertTrue(backup.is_file(), "corrupt transcript must be backed up")
        self.assertEqual(backup.read_text(encoding="utf-8"), "{ this is not json")
        self.assertFalse(path.exists())

        status, data, _raw = self.request(
            "GET", "/api/jobs/{}/transcript".format(job_id))
        self.assertEqual(status, 404, "later requests must not 500 again")
        self.assertEqual(data["error"]["code"], "http_404")


# ------------------------------------------- R2: ffmpeg side log cleanup


class DeleteFfmpegLogTest(EngineApiTest):
    """DELETE /jobs left <id>.wav.ffmpeg.log in data/cache for ever."""

    def test_delete_removes_the_ffmpeg_side_log(self):
        job_id = self.make_job()
        side_log = self.state.paths.cache_dir / "{}.wav.ffmpeg.log".format(job_id)
        side_log.write_text("ffmpeg stderr", encoding="utf-8")

        status, data, _raw = self.request(
            "DELETE", "/api/jobs/{}".format(job_id))

        self.assertEqual(status, 200, data)
        self.assertFalse(side_log.exists(), "ffmpeg side log survived delete")


# ------------------------------------------- R2: OOM requeue vs cancel


class OomRetrySerialisationTest(EngineApiTest):
    """The OOM requeue (error -> queued) ran outside lifecycle_lock.

    A /cancel landing in that window only recorded cancel_requested; the worker
    then discarded it while claiming the requeued job, so the job ran again
    against the user's explicit cancel.
    """

    def test_requeue_is_serialised_with_a_concurrent_cancel(self):
        job_id = self.make_job()
        state = self.state
        state.store.update_job(job_id, status="error", error="oom: 显存不足")
        entered = threading.Event()
        release = threading.Event()
        real_update = state.store.update_job
        replies = {}
        outcome = {}

        def blocking_update(target, **fields):
            if fields.get("status") == "queued" and not entered.is_set():
                entered.set()
                release.wait(20)
            return real_update(target, **fields)

        def requeue():
            outcome["requeued"] = state.worker._schedule_oom_retry(
                state.store.get_job(job_id), RuntimeError("CUDA out of memory"))

        def cancel():
            replies["cancel"] = self.request(
                "POST", "/api/jobs/{}/cancel".format(job_id))

        state.store.update_job = blocking_update
        try:
            requeuer = threading.Thread(target=requeue, name="vt-oom-requeue",
                                        daemon=True)
            requeuer.start()
            self.assertTrue(entered.wait(10), "requeue never reached the write")
            canceller = threading.Thread(target=cancel, name="vt-oom-cancel",
                                         daemon=True)
            canceller.start()
            time.sleep(0.4)
            self.assertTrue(
                canceller.is_alive(),
                "/cancel must wait for the OOM requeue (same lifecycle_lock)")
            release.set()
            requeuer.join(20)
            canceller.join(20)
        finally:
            release.set()
            try:
                del state.store.update_job
            except AttributeError:
                pass

        self.assertTrue(outcome.get("requeued"))
        self.assertEqual(replies["cancel"][0], 200, replies.get("cancel"))
        self.assertEqual(state.store.get_job(job_id)["status"], "canceled",
                         "the cancel must win over the automatic requeue")


# ------------------------------------------- R2: English word rebuild


class EnglishWordRebuildTest(unittest.TestCase):
    """Editing an English sentence merged every word into one blob.

    rebuild_words matched latin runs on the punctuation-free, space-free
    character stream, so "This is a long English ok" came back as the single
    word "ThisisalongEnglishok" (SPEC 3.2: English is one word per word).
    """

    @staticmethod
    def _sentence(words):
        return {
            "id": 0,
            "start_ms": 0,
            "end_ms": 3000,
            "text": " ".join(words),
            "conf": 0.9,
            "edited": False,
            "words": [{"text": word, "start_ms": 100 + i * 400,
                       "end_ms": 500 + i * 400}
                      for i, word in enumerate(words)],
        }

    def test_edited_english_keeps_word_boundaries(self):
        sentence = self._sentence(["This", "is", "a", "test"])
        edit.set_sentence_text(sentence, "This is a long English ok")
        texts = [word["text"] for word in sentence["words"]]
        self.assertEqual(texts, ["This", "is", "a", "long", "English", "ok"])
        self.assertEqual("".join(texts), textutil.content_text(sentence["text"]))
        for word in sentence["words"]:
            self.assertGreater(word["end_ms"], word["start_ms"])

    def test_punctuation_and_cjk_boundaries_are_kept(self):
        sentence = self._sentence(["你好", "世界"])
        edit.set_sentence_text(sentence, "Hello, world 世界")
        texts = [word["text"] for word in sentence["words"]]
        self.assertEqual(texts, ["Hello", "world", "世", "界"])
        self.assertEqual("".join(texts), textutil.content_text(sentence["text"]))


if __name__ == "__main__":
    unittest.main()
