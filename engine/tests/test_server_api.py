"""HTTP / WebSocket contract tests for the engine server.

Design notes
------------
* No model is ever loaded: the app runs on a stub runtime and probe_gpu is
  patched out, so nothing imports torch (the /system handler would otherwise
  probe the GPU).
* httpx / fastapi.testclient is not installed, so a real uvicorn server is
  started on an ephemeral 127.0.0.1 port and driven with stdlib http.client
  (plus websockets for the one WS test).
* Data lives under <project>/_scratch/<YYYY-MM-DD>/ so the suite never writes
  outside the workspace, and every class removes the directory it created.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import socket
import threading
import time
import unittest
import urllib.parse
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import uvicorn

from voicetrace_engine import server as engine_server
from voicetrace_engine.config import DEFAULT_SETTINGS
from voicetrace_engine.media import MediaInfo
from voicetrace_engine.pipeline import Cancelled, JobError, Pipeline
from voicetrace_engine.segmentation import SEG_VERSION
from voicetrace_engine.store import Store

PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ helpers


def scratch_dir(tag: str) -> Path:
    """Create a throw-away directory inside the workspace _scratch tree."""
    base = PROJECT_ROOT / "_scratch" / time.strftime("%Y-%m-%d") / "engine-tests"
    path = base / "{}-{}".format(tag, uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_scratch(path: Path) -> None:
    """Remove a directory only if this suite created it under _scratch."""
    target = Path(path).resolve()
    if PROJECT_ROOT not in target.parents or "_scratch" not in target.parts:
        return
    shutil.rmtree(target, ignore_errors=True)
    try:
        target.parent.rmdir()  # only removes the shared parent when empty
    except OSError:
        pass


class StubRuntime:
    """Minimal stand-in for voicetrace_engine.runtime.Runtime (no torch)."""

    def __init__(self, ready: bool = True):
        self.ready = ready
        self.loading = False
        self.error = None
        self.device = "cpu"
        self.use_gpu = False
        self.settings = dict(DEFAULT_SETTINGS)
        self.load_calls = 0
        self.on_load = None

    def load(self, progress=None):
        self.load_calls += 1
        self.loading = True
        try:
            if self.on_load is not None:
                self.on_load()
            if progress is not None:
                progress(1.0, "引擎就绪")
            self.ready = True
            self.error = None
        finally:
            self.loading = False


def make_sentence(sid, text, start_ms=1000):
    words = []
    for i, ch in enumerate(text):
        if ch in "，。！？、,.!?":
            continue
        words.append({"text": ch, "start_ms": start_ms + i * 120,
                      "end_ms": start_ms + (i + 1) * 120})
    return {
        "id": sid,
        "start_ms": start_ms,
        "end_ms": start_ms + max(1, len(text)) * 120,
        "text": text,
        "conf": 0.9,
        "words": words,
        "edited": False,
    }


def make_transcript(count=2):
    sentences = [make_sentence(i, "第{}句测试文本。".format(i + 1), 1000 + i * 2000)
                 for i in range(count)]
    return {
        "job_id": "stub",
        "version": 1,
        "seg_version": SEG_VERSION,
        "media": {"name": "clip.mp4", "path": "clip.mp4",
                  "duration_ms": max(1, count) * 2000, "width": 1280, "height": 720},
        "sentences": sentences,
        "dismissed": [],
        "stats": {},
    }


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


# ------------------------------------------------------------------- fixture


class EngineServerTest(unittest.TestCase):
    """Base class: one uvicorn server + temp data dir per test class."""

    token = "test-token-0123456789"

    @classmethod
    def setUpClass(cls):
        cls.tmp = scratch_dir("server-api")
        data, models, bin_dir = make_env(cls.tmp)
        args = engine_server.parse_args([
            "--token", cls.token,
            "--data-dir", str(data),
            "--models-dir", str(models),
            "--ffmpeg", str(bin_dir),
        ])
        cls.state = engine_server.State(args)
        cls.runtime = StubRuntime(ready=True)
        cls.state.runtime = cls.runtime
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
                                      name="vt-test-server", daemon=True)
        cls.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline and not cls.server.started:
            if not cls.thread.is_alive():
                break
            time.sleep(0.02)
        if not cls.server.started:
            raise RuntimeError("uvicorn did not start for the API tests")

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
    def request(self, method, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        payload = None
        hdrs = dict(headers or {})
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if token:
            hdrs["Authorization"] = "Bearer " + self.token
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

    def make_job(self, count=2):
        src = self.tmp / "media" / "clip-{}.mp4".format(uuid.uuid4().hex[:8])
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"stub-media")
        job = self.state.store.add_job(str(src), src.name, src.stat().st_size)
        transcript = make_transcript(count)
        transcript["job_id"] = job["id"]
        transcript["media"]["name"] = src.name
        transcript["media"]["path"] = str(src)
        self.state.save_transcript(job["id"], transcript)
        return job["id"]


# ------------------------------------------------------------------- tests


class AuthAndEnvelopeTest(EngineServerTest):
    def test_health_is_public(self):
        status, data, _raw = self.request("GET", "/api/health", token=False)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_health_alias_skips_auth(self):
        # /health has no route, but it must not answer 401 either.
        status, data, _raw = self.request("GET", "/health", token=False)
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "http_404")

    def test_system_requires_token(self):
        status, data, _raw = self.request("GET", "/api/system", token=False)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "unauthorized")

    def test_system_rejects_wrong_token(self):
        status, data, _raw = self.request("GET", "/api/system", token=False,
                                         headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "unauthorized")

    def test_query_token_is_limited_to_media_endpoints(self):
        # A token in the URL leaks via history / Referer, so only the two
        # resource endpoints (and the websocket) accept it.
        status, data, _raw = self.request(
            "GET", "/api/system?token=" + self.token, token=False)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "unauthorized")

        job_id = self.make_job(1)
        thumb = self.state.paths.thumb_dir / (job_id + ".jpg")
        thumb.write_bytes(b"\xff\xd8\xff\xe0q")
        status, _data, _raw = self.request(
            "GET", "/api/jobs/{}/thumb?token={}".format(job_id, self.token),
            token=False)
        self.assertEqual(status, 200)

    def test_unknown_route_envelope(self):
        status, data, _raw = self.request("GET", "/api/no-such-endpoint")
        self.assertEqual(status, 404)
        self.assertEqual(set(data["error"]), {"code", "message"})
        self.assertEqual(data["error"]["code"], "http_404")

    def test_method_not_allowed_envelope(self):
        status, data, _raw = self.request("PUT", "/api/jobs")
        self.assertEqual(status, 405)
        self.assertEqual(data["error"]["code"], "http_405")
        self.assertTrue(data["error"]["message"])

    def test_validation_error_envelope(self):
        status, data, _raw = self.request("POST", "/api/jobs",
                                         {"paths": "not-a-list"})
        self.assertEqual(status, 422)
        self.assertEqual(data["error"]["code"], "validation_error")
        self.assertTrue(data["error"]["message"])

    def test_missing_token_exits_with_stderr(self):
        buffer = io.StringIO()
        args = engine_server.parse_args([
            "--data-dir", str(self.tmp / "no-token"),
            "--models-dir", str(self.tmp / "models"),
            "--ffmpeg", str(self.tmp / "bin"),
        ])
        with mock.patch.dict(os.environ, {"VT_TOKEN": ""}), redirect_stderr(buffer):
            with self.assertRaises(SystemExit) as caught:
                engine_server.State(args)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("engine: missing token", buffer.getvalue())


class SystemPayloadTest(EngineServerTest):
    def test_system_shape(self):
        # 夹具的 models 目录是空的（测试不可能放几 GB 真模型），而
        # models_present 按文件就绪检查：本用例只验证 payload 结构与接线，
        # 把文件检查桩成就绪。旧实现断言全 True 却不给桩，出厂即失败。
        with mock.patch.object(engine_server, "is_model_ready", return_value=True):
            status, data, _raw = self.request("GET", "/api/system")
        self.assertEqual(status, 200)
        self.assertEqual(data["model_state"], "ready")
        self.assertEqual(data["models_present"],
                         {"aed": True, "vad": True, "punc": True})
        self.assertEqual(data["sample_rate"], 16000)
        self.assertIn(data["device"], ("cpu", "cuda"))
        self.assertNotIn("models", data, "old models field must be renamed")
        for key in ("version", "ready", "loading", "error", "gpu", "queue",
                    "paths", "ffmpeg", "uptime_s"):
            self.assertIn(key, data)

    def test_model_state_mapping(self):
        flags = mock.Mock(loading=False, ready=False, error=None)
        self.assertEqual(engine_server.model_state(flags), "idle")
        flags.loading = True
        self.assertEqual(engine_server.model_state(flags), "loading")
        flags.loading = False
        flags.ready = True
        self.assertEqual(engine_server.model_state(flags), "ready")
        flags.ready = False
        flags.error = "boom"
        self.assertEqual(engine_server.model_state(flags), "failed")


class ThumbTest(EngineServerTest):
    def test_unknown_job_is_404(self):
        status, data, _raw = self.request("GET", "/api/jobs/deadbeef/thumb")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "http_404")

    def test_traversal_id_never_leaves_thumb_dir(self):
        decoy = self.tmp / "decoy"
        decoy.mkdir(parents=True, exist_ok=True)
        (decoy / "evil.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")
        job_id = r"..\..\decoy\evil"
        encoded = urllib.parse.quote(job_id, safe="")
        status, data, _raw = self.request("GET", "/api/jobs/" + encoded + "/thumb")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "http_404")

    def test_existing_thumb_is_served(self):
        job_id = self.make_job(1)
        thumb = self.state.paths.thumb_dir / (job_id + ".jpg")
        thumb.write_bytes(b"\xff\xd8\xff\xe0real")
        self.state.store.update_job(job_id, thumb_path=str(thumb))
        status, _data, raw = self.request("GET", "/api/jobs/" + job_id + "/thumb")
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\xff\xd8"))


class ExportTest(EngineServerTest):
    def test_extension_must_match_format(self):
        job_id = self.make_job(1)
        target = self.tmp / "exports" / "out.txt"
        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/export".format(job_id),
            {"format": "srt", "path": str(target)})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "http_400")
        self.assertFalse(target.exists())

    def test_relative_path_is_rejected(self):
        job_id = self.make_job(1)
        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/export".format(job_id),
            {"format": "srt", "path": "relative/out.srt"})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "http_400")

    def test_reserved_device_name_is_rejected(self):
        job_id = self.make_job(1)
        target = self.tmp / "exports" / "CON.srt"
        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/export".format(job_id),
            {"format": "srt", "path": str(target)})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "http_400")

    def test_export_writes_through_a_temp_file(self):
        job_id = self.make_job(1)
        target = self.tmp / "exports" / "good.srt"
        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/export".format(job_id),
            {"format": "srt", "path": str(target)})
        self.assertEqual(status, 200)
        self.assertEqual(Path(data["path"]).resolve(), target.resolve())
        self.assertFalse(data["fallback"])
        text = target.read_text(encoding="utf-8-sig")
        self.assertIn("-->", text)
        leftovers = [p.name for p in target.parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "atomic write must clean its temp file")

    def test_default_path_when_omitted(self):
        job_id = self.make_job(1)
        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/export".format(job_id), {"format": "txt"})
        self.assertEqual(status, 200)
        written = Path(data["path"]).resolve()
        self.assertEqual(written.parent,
                         (self.state.paths.data_dir / "exports").resolve())
        self.assertTrue(written.is_file())


class SettingsRoundTripTest(EngineServerTest):
    def test_round_trip(self):
        status, before, _raw = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("subtitle_max_chars", before)
        try:
            status, data, _raw = self.request(
                "PUT", "/api/settings", {"subtitle_max_chars": 30})
            self.assertEqual(status, 200)
            self.assertEqual(data["subtitle_max_chars"], 30)
            status, after, _raw = self.request("GET", "/api/settings")
            self.assertEqual(after["subtitle_max_chars"], 30)
            status, data, _raw = self.request(
                "PUT", "/api/settings", {"settings": {"subtitle_max_chars": 22}})
            self.assertEqual(status, 200)
            self.assertEqual(data["subtitle_max_chars"], 22)
        finally:
            self.request("PUT", "/api/settings",
                         {"subtitle_max_chars": before["subtitle_max_chars"]})
        saved = json.loads(self.state.paths.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["subtitle_max_chars"], before["subtitle_max_chars"])


class ImportContractTest(EngineServerTest):
    def test_import_returns_jobs_and_skipped(self):
        status, data, _raw = self.request("POST", "/api/jobs", {
            "paths": [str(self.tmp / "missing.mp4"), "relative-missing.mp4"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["jobs"], [])
        self.assertEqual(len(data["skipped"]), 2)
        for item in data["skipped"]:
            self.assertIn("path", item)
            self.assertIn("reason", item)

    def test_import_enables_auto_correct_by_default(self):
        src = self.tmp / "media" / "auto-on-{}.mp4".format(uuid.uuid4().hex[:8])
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"stub-media")
        status, data, _raw = self.request("POST", "/api/jobs",
                                          {"paths": [str(src)]})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["jobs"]), 1)
        self.assertTrue(data["jobs"][0]["auto_correct"])

        src2 = self.tmp / "media" / "auto-off-{}.mp4".format(uuid.uuid4().hex[:8])
        src2.write_bytes(b"stub-media")
        status, data, _raw = self.request(
            "POST", "/api/jobs",
            {"paths": [str(src2)], "auto_correct": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["jobs"][0]["auto_correct"])


class AutoExportTest(EngineServerTest):
    """转录完成后自动把 SRT 写到视频所在目录（auto_export 默认开）。"""

    def _job_and_transcript(self):
        job_id = self.make_job(2)
        job = self.state.store.get_job(job_id)
        transcript = make_transcript(2)
        transcript["job_id"] = job_id
        return job, transcript

    def _export(self, job, transcript, enabled=True):
        before = self.state.settings.get("auto_export")
        self.state.settings["auto_export"] = enabled
        try:
            engine_server.Worker(self.state)._auto_export_srt(job, transcript)
        finally:
            self.state.settings["auto_export"] = before

    def test_srt_lands_next_to_the_video(self):
        job, transcript = self._job_and_transcript()
        self._export(job, transcript)
        target = Path(job["path"]).with_suffix(".srt")
        self.assertTrue(target.is_file(), "自动导出的 srt 应和视频同目录")
        self.assertIn("-->", target.read_text(encoding="utf-8-sig"))
        leftovers = [p.name for p in target.parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "原子写必须清理临时文件")

    def test_existing_srt_is_never_overwritten(self):
        job, transcript = self._job_and_transcript()
        target = Path(job["path"]).with_suffix(".srt")
        target.write_text("原有字幕", encoding="utf-8")
        self._export(job, transcript)
        self.assertEqual(target.read_text(encoding="utf-8"), "原有字幕")
        second = target.with_name(target.stem + " (2).srt")
        self.assertTrue(second.is_file(), "同名文件已存在时必须追加序号")

    def test_disabled_setting_writes_nothing(self):
        job, transcript = self._job_and_transcript()
        self._export(job, transcript, enabled=False)
        target = Path(job["path"]).with_suffix(".srt")
        self.assertFalse(target.exists())


class ConcurrentEditTest(EngineServerTest):
    def test_eight_parallel_puts_keep_every_sentence(self):
        job_id = self.make_job(8)
        new_texts = {i: "并发写入第{}句的内容。".format(i + 1) for i in range(8)}
        errors = []
        barrier = threading.Barrier(8)

        def worker(index):
            try:
                barrier.wait(timeout=20)
                status, data, _raw = self.request(
                    "PUT", "/api/jobs/{}/sentences/{}".format(job_id, index),
                    {"text": new_texts[index]})
                if status != 200:
                    errors.append((index, status, data))
            except Exception as exc:  # noqa: BLE001
                errors.append((index, repr(exc)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])

        status, transcript, _raw = self.request(
            "GET", "/api/jobs/{}/transcript".format(job_id))
        self.assertEqual(status, 200)
        self.assertEqual(len(transcript["sentences"]), 8, "sentences were lost")
        texts = {s["id"]: s["text"] for s in transcript["sentences"]}
        for index in range(8):
            self.assertEqual(texts[index], new_texts[index],
                             "concurrent edit of sentence {} was overwritten".format(index))


class VersionCasTest(EngineServerTest):
    def test_stale_expected_version_is_rejected(self):
        job_id = self.make_job(2)
        status, data, _raw = self.request(
            "PUT", "/api/jobs/{}/sentences/0".format(job_id),
            {"text": "改写第一句。", "expected_version": 999})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "http_409")
        status, transcript, _raw = self.request(
            "GET", "/api/jobs/{}/transcript".format(job_id))
        self.assertEqual(transcript["version"], 1, "rejected write must not apply")

        status, data, _raw = self.request(
            "PUT", "/api/jobs/{}/sentences/0".format(job_id),
            {"text": "改写第一句。", "expected_version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(data["transcript"]["version"], 2)

        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/undo?expected_version=1".format(job_id))
        self.assertEqual(status, 409)
        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/undo?expected_version=2".format(job_id))
        self.assertEqual(status, 200)
        self.assertTrue(data["undone"])


class WebSocketSnapshotTest(EngineServerTest):
    def test_snapshot_is_the_first_frame(self):
        try:
            from websockets.sync.client import connect as ws_connect
        except Exception as exc:  # noqa: BLE001 - optional client dependency
            self.skipTest("websockets client unavailable: {}".format(exc))
        job_id = self.make_job(1)
        url = "ws://127.0.0.1:{}/ws?token={}".format(self.port, self.token)
        with ws_connect(url, open_timeout=20) as ws:
            first = json.loads(ws.recv(timeout=20))
            self.assertEqual(first["type"], "snapshot")
            self.assertIn("queue", first)
            self.assertIn("engine", first)
            self.assertEqual(first["engine"]["sample_rate"], 16000)
            self.assertIsInstance(first["jobs"], list)
            self.assertIn(job_id, [job["id"] for job in first["jobs"]])

            # incremental events keep flowing after the snapshot
            self.state.hub.publish({"type": "toast", "level": "info",
                                    "message": "hello"})
            nxt = json.loads(ws.recv(timeout=20))
            self.assertEqual(nxt["type"], "toast")
            self.assertEqual(nxt["message"], "hello")


class PipelineStageTest(unittest.TestCase):
    """C6: the first job must publish stage=load before it probes anything."""

    def setUp(self):
        self.tmp = scratch_dir("pipeline-stage")
        self.data, self.models, self.bin_dir = make_env(self.tmp)
        args = engine_server.parse_args([
            "--token", "stage-token",
            "--data-dir", str(self.data),
            "--models-dir", str(self.models),
            "--ffmpeg", str(self.bin_dir),
        ])
        self.state = engine_server.State(args)
        self.store = self.state.store

    def tearDown(self):
        try:
            self.store.close()
        finally:
            remove_scratch(self.tmp)

    def test_load_stage_runs_before_probe(self):
        runtime = StubRuntime(ready=False)
        pipeline = Pipeline(self.state.paths, self.store, dict(DEFAULT_SETTINGS),
                            runtime, emit=None)
        job = self.store.add_job(str(self.tmp / "clip.mp4"), "clip.mp4", 0)
        observed = {}
        reports = []
        real_report = pipeline._report

        def spy(job_id, stage, progress, message, force=False):
            reports.append(stage)
            return real_report(job_id, stage, progress, message, force)

        def on_load():
            observed["stage_during_load"] = self.store.get_job(job["id"])["stage"]

        def fake_run(job_row, t0, cancel):
            observed["stage_at_run"] = self.store.get_job(job_row["id"])["stage"]
            raise Cancelled()

        runtime.on_load = on_load
        pipeline._report = spy
        pipeline._run = fake_run
        self.assertIsNone(pipeline.run(job["id"]))

        self.assertEqual(observed["stage_during_load"], "load")
        self.assertEqual(observed["stage_at_run"], "probe")
        # every report before probe belongs to the load stage
        self.assertEqual(reports[0], "load")
        self.assertIn("probe", reports)
        probe_index = reports.index("probe")
        self.assertEqual(reports[:probe_index], ["load"] * probe_index)
        self.assertEqual(runtime.load_calls, 1)
        self.assertEqual(self.store.get_job(job["id"])["status"], "canceled")


class NoAudioJobTest(unittest.TestCase):
    """T8: a file without an audio track fails with a readable code."""

    def setUp(self):
        self.tmp = scratch_dir("no-audio")
        self.data, self.models, self.bin_dir = make_env(self.tmp)
        args = engine_server.parse_args([
            "--token", "no-audio-token",
            "--data-dir", str(self.data),
            "--models-dir", str(self.models),
            "--ffmpeg", str(self.bin_dir),
        ])
        self.state = engine_server.State(args)
        self.store = self.state.store

    def tearDown(self):
        try:
            self.store.close()
        finally:
            remove_scratch(self.tmp)

    def test_no_audio_track_sets_error_code(self):
        pipeline = Pipeline(self.state.paths, self.store, dict(DEFAULT_SETTINGS),
                            StubRuntime(ready=True), emit=None)
        # The file must exist: a missing file is now reported as file_missing
        # (a different, more accurate error), while this test pins the
        # audio-less-but-present case.
        silent = self.tmp / "silent.mp4"
        silent.write_bytes(b"")
        job = self.store.add_job(str(silent), "silent.mp4", 0)
        with mock.patch("voicetrace_engine.pipeline.probe",
                        lambda _ffprobe, _src: MediaInfo(has_audio=False)):
            with self.assertRaises(JobError):
                pipeline.run(job["id"])
        row = self.store.get_job(job["id"])
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["error"].startswith("no_audio:"), row["error"])
        self.assertIn("该文件没有音轨", row["error"])



class MigrationPreservesEditTest(EngineServerTest):
    """A1: the legacy migration must not roll back a newer manual edit."""

    def test_edited_sentence_survives_migration_and_version_advances(self):
        job_id = self.make_job(1)
        path = self.state.transcript_path(job_id)
        old = json.loads(path.read_text(encoding="utf-8"))
        old["seg_version"] = 0
        old["version"] = 2
        text = "人工保存的新字幕"
        old["sentences"] = [{
            "id": 0, "start_ms": 1000, "end_ms": 1000 + len(text) * 100,
            "text": text, "conf": 1.0, "edited": True,
            "words": [{"text": ch, "start_ms": 1000 + i * 100,
                       "end_ms": 1100 + i * 100} for i, ch in enumerate(text)],
        }]
        path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

        status, data, _raw = self.request(
            "GET", "/api/jobs/{}/transcript".format(job_id))
        self.assertEqual(status, 200)
        self.assertEqual(data["sentences"][0]["text"], text)
        self.assertEqual(data["seg_version"], SEG_VERSION)
        self.assertEqual(data["version"], 3, "migration must bump the version")


class UndoFailureTest(EngineServerTest):
    """A8: a failed undo write must keep the snapshot for a retry."""

    def test_failed_undo_keeps_history_and_is_retryable(self):
        job_id = self.make_job(1)
        status, _data, _raw = self.request(
            "PUT", "/api/jobs/{}/sentences/0".format(job_id),
            {"text": "撤销前的修改"})
        self.assertEqual(status, 200)
        status, hist, _raw = self.request(
            "GET", "/api/jobs/{}/history".format(job_id))
        self.assertEqual(hist["count"], 1)

        with mock.patch.object(self.state, "save_transcript",
                               side_effect=OSError("disk full")):
            status, _data, _raw = self.request(
                "POST", "/api/jobs/{}/undo".format(job_id))
        self.assertEqual(status, 500)
        status, hist, _raw = self.request(
            "GET", "/api/jobs/{}/history".format(job_id))
        self.assertEqual(hist["count"], 1, "A8: failed undo consumed history")

        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/undo".format(job_id))
        self.assertEqual(status, 200)
        self.assertTrue(data["undone"])
        status, hist, _raw = self.request(
            "GET", "/api/jobs/{}/history".format(job_id))
        self.assertEqual(hist["count"], 0)


class SettingsFailureTest(EngineServerTest):
    """A9: a failed settings write must leave the live config untouched."""

    def test_failed_save_keeps_previous_settings(self):
        status, before, _raw = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        batch = before["batch_size"]
        with mock.patch("voicetrace_engine.server.save_settings",
                        side_effect=OSError("disk full")):
            status, _data, _raw = self.request(
                "PUT", "/api/settings", {"batch_size": 3})
        self.assertEqual(status, 500)
        status, after, _raw = self.request("GET", "/api/settings")
        self.assertEqual(after, before, "A9: failed write cleared the settings")

        status, data, _raw = self.request("PUT", "/api/settings", {"batch_size": 3})
        self.assertEqual(status, 200)
        self.assertEqual(data["batch_size"], 3)
        self.request("PUT", "/api/settings", {"batch_size": batch})


class ExportCollisionTest(EngineServerTest):
    """C8: two jobs with the same basename must not overwrite each other."""

    def _job_with_name(self, sub, name):
        src = self.tmp / sub / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"stub-media")
        job = self.state.store.add_job(str(src), name, src.stat().st_size)
        transcript = make_transcript(1)
        transcript["job_id"] = job["id"]
        transcript["media"]["name"] = name
        transcript["media"]["path"] = str(src)
        self.state.save_transcript(job["id"], transcript)
        return job["id"]

    def test_default_export_names_do_not_collide(self):
        first = self._job_with_name("one", "lesson.mp4")
        second = self._job_with_name("two", "lesson.mp4")
        status, a, _raw = self.request(
            "POST", "/api/jobs/{}/export".format(first), {"format": "srt"})
        self.assertEqual(status, 200)
        status, b, _raw = self.request(
            "POST", "/api/jobs/{}/export".format(second), {"format": "srt"})
        self.assertEqual(status, 200)
        self.assertNotEqual(Path(a["path"]).resolve(), Path(b["path"]).resolve())
        self.assertTrue(Path(a["path"]).is_file())
        self.assertTrue(Path(b["path"]).is_file())


class ImportDedupTest(EngineServerTest):
    """C9: the same path twice in one batch creates one job."""

    def test_same_path_twice_is_skipped_once(self):
        src = self.tmp / "dup" / "same.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"stub-media")
        status, data, _raw = self.request(
            "POST", "/api/jobs", {"paths": [str(src), str(src)]})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["jobs"]), 1)
        self.assertEqual(len(data["skipped"]), 1)


class SuffixRangeTest(EngineServerTest):
    """C10: bytes=-N returns the last N bytes."""

    def test_suffix_range_returns_tail(self):
        src = self.tmp / "range" / "blob.bin"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"0123456789")
        job = self.state.store.add_job(str(src), src.name, 10)
        status, _data, raw = self.request(
            "GET", "/api/jobs/{}/media".format(job["id"]),
            headers={"Range": "bytes=-4"})
        self.assertEqual(status, 206)
        self.assertEqual(raw, b"6789")


class CorsOriginTest(EngineServerTest):
    """C10: only the desktop/dev origins may read the API cross-origin."""

    def _options(self, origin):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("OPTIONS", "/api/jobs", headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Authorization": "Bearer " + self.token,
            })
            response = conn.getresponse()
            response.read()
            return response.status, response.getheader("Access-Control-Allow-Origin")
        finally:
            conn.close()

    def test_untrusted_origin_gets_no_allow_origin(self):
        _status, allow = self._options("https://evil.example")
        self.assertIsNone(allow)

    def test_tauri_origin_is_allowed(self):
        _status, allow = self._options("http://tauri.localhost")
        self.assertEqual(allow, "http://tauri.localhost")


class CancelClaimTest(EngineServerTest):
    """B3: a canceled queued job must not be resurrected by the pipeline."""

    def test_pipeline_claim_refuses_canceled_job(self):
        job_id = self.make_job(1)
        status, data, _raw = self.request(
            "POST", "/api/jobs/{}/cancel".format(job_id))
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "canceled")
        result = self.state.pipeline.run(job_id, threading.Event())
        self.assertIsNone(result)
        self.assertEqual(self.state.store.get_job(job_id)["status"], "canceled")


class HistoryDepthTest(EngineServerTest):
    """C4: undo depth is a server fact."""

    def test_history_count_tracks_edits_and_undo(self):
        job_id = self.make_job(1)
        status, data, _raw = self.request(
            "GET", "/api/jobs/{}/history".format(job_id))
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 0)
        self.request("PUT", "/api/jobs/{}/sentences/0".format(job_id),
                     {"text": "第一次修改"})
        status, data, _raw = self.request(
            "GET", "/api/jobs/{}/history".format(job_id))
        self.assertEqual(data["count"], 1)
        self.request("POST", "/api/jobs/{}/undo".format(job_id))
        status, data, _raw = self.request(
            "GET", "/api/jobs/{}/history".format(job_id))
        self.assertEqual(data["count"], 0)

if __name__ == "__main__":
    unittest.main()
