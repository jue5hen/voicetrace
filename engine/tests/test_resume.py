"""续跑（增量落盘）、媒体指纹校验与 OOM 自动降级的回归测试。

设计
----
* 一律用桩 Runtime：本文件不 import torch、不加载模型、不碰 GPU。
* 素材是 soundfile 现场写的 16 kHz 单声道 wav，probe / extract_audio /
  make_thumb 全部打桩，所以既不依赖 ffmpeg 也不读真实视频。
* 数据落在 <项目>\_scratch\<日期>\engine-tests\ 下，用完即删。
"""
from __future__ import annotations

import http.client
import json
import shutil
import socket
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf
import uvicorn

from voicetrace_engine import server as engine_server
from voicetrace_engine.config import DEFAULT_SETTINGS, Paths
from voicetrace_engine.media import MediaInfo
from voicetrace_engine.pipeline import OOM_MESSAGE, Pipeline, is_out_of_memory
from voicetrace_engine.runtime import AsrItem, AsrResult, Token
from voicetrace_engine.store import Store

try:  # 契约 2 的异常由 runtime.py 提供；对方还没落地时用同形替身兜底
    from voicetrace_engine.runtime import AsrOutOfMemory
except ImportError:  # pragma: no cover - 只在并行开发期间可能走到
    class AsrOutOfMemory(RuntimeError):  # type: ignore[no-redef]
        """AsrOutOfMemory 落地前的同形替身：带 batch_size，文本可识别。"""

        def __init__(self, batch_size: int = 1) -> None:
            super().__init__(f"CUDA 显存不足：单段识别仍然 out of memory (batch={batch_size})")
            self.batch_size = int(batch_size)


PROJECT_ROOT = Path(__file__).resolve().parents[3]

SAMPLE_RATE = 16000
DURATION_MS = 5000
# 5 段各 800ms，间隔 200ms：_normalize_segments 不会把它们合并
SEGMENTS = [(0, 800), (1000, 1800), (2000, 2800), (3000, 3800), (4000, 4800)]


# ------------------------------------------------------------------ helpers


def scratch_dir(tag: str) -> Path:
    """在 <项目>\_scratch\<日期>\engine-tests 下建一个一次性目录。"""
    base = PROJECT_ROOT / "_scratch" / time.strftime("%Y-%m-%d") / "engine-tests"
    path = base / "{}-{}".format(tag, uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_scratch(path: Path) -> None:
    """只删本测试自己在 _scratch 下建的目录（防止误删工作区）。"""
    target = Path(path).resolve()
    if PROJECT_ROOT not in target.parents or "_scratch" not in target.parts:
        return
    shutil.rmtree(target, ignore_errors=True)
    try:
        target.parent.rmdir()  # 共享父目录空了才删得掉
    except OSError:
        pass


def write_wav(path: Path, duration_ms: int = DURATION_MS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(SAMPLE_RATE * duration_ms / 1000)
    # 低幅噪声足够：桩 VAD 不看内容，只返回固定段落
    audio = (np.random.default_rng(7).standard_normal(frames) * 200).astype(np.int16)
    sf.write(str(path), audio, SAMPLE_RATE, subtype="PCM_16")


def fake_probe(_ffprobe, src) -> MediaInfo:
    return MediaInfo(duration_ms=DURATION_MS, width=1280, height=720,
                     has_audio=True, size_bytes=Path(src).stat().st_size)


def fake_extract(_ffmpeg, _src, dst, _duration_ms=0,
                 on_progress=None, cancel=None) -> None:
    write_wav(Path(dst))


def patch_media():
    """把 probe / extract_audio / make_thumb 换成桩，测试不需要 ffmpeg。"""
    return mock.patch.multiple(
        "voicetrace_engine.pipeline",
        probe=fake_probe,
        extract_audio=fake_extract,
        make_thumb=lambda *a, **k: None,
    )


def oom_error(batch_size: int = 1) -> BaseException:
    """构造契约里的显存不足异常（优先用 runtime.py 的真家伙）。"""
    try:
        return AsrOutOfMemory(batch_size)
    except TypeError:  # pragma: no cover - 真类构造签名变了才会走到
        err = RuntimeError(f"CUDA 显存不足：单段识别仍然 out of memory (batch={batch_size})")
        err.code = "oom"
        return err


def load_transcript(paths: Paths, job_id: str) -> dict:
    path = paths.transcript_dir / "{}.json".format(job_id)
    return json.loads(path.read_text(encoding="utf-8"))


class StubRuntime:
    """确定性桩：VAD 返回固定段落，ASR 按批吐 AsrResult，标点按 4 token 断句。"""

    def __init__(self, batch_fail_after: int | None = None,
                 fail_with: BaseException | None = None) -> None:
        self.ready = True
        self.loading = False
        self.error = None
        self.device = "cpu"
        self.use_gpu = False
        self.settings = dict(DEFAULT_SETTINGS)
        self.segments = list(SEGMENTS)
        self.batch_fail_after = batch_fail_after
        self.fail_with = fail_with
        # 桩自己的批上限，模拟「真 runtime 会缩批」：让第一批只落盘一部分
        self.max_chunk = 2
        self.seen_keys: list[str] = []      # 真正被送去识别的 key（跨调用累计）
        self.seen_batches: list[int] = []   # 每次 transcribe 收到的 batch_size
        self.batch_calls = 0

    def load(self, progress=None) -> None:
        if progress is not None:
            progress(1.0, "引擎就绪")
        self.ready = True

    def detect(self, _audio):
        return list(self.segments)

    def transcribe(self, items, batch_size=6, progress=None, cancelled=None,
                   on_batch=None):
        self.seen_batches.append(int(batch_size))
        self.batch_calls = 0
        chunk = max(1, min(int(batch_size), self.max_chunk))
        results: list[AsrResult] = []
        for start in range(0, len(items), chunk):
            batch = list(items[start:start + chunk])
            out = [self._result(it) for it in batch]
            results.extend(out)
            self.seen_keys.extend(it.key for it in batch)
            if progress is not None:
                progress(len(results) / max(len(items), 1),
                         "识别 {}/{} 段".format(len(results), len(items)))
            if on_batch is not None:
                self.batch_calls += 1
                on_batch(out)
            if (self.batch_fail_after is not None
                    and self.batch_calls >= self.batch_fail_after):
                raise self.fail_with or RuntimeError("boom")
        return results

    def punctuate(self, tokens, progress=None, cancelled=None):
        sentences: list[dict] = []
        chunk: list[Token] = []
        for token in tokens:
            chunk.append(token)
            if len(chunk) >= 4:
                sentences.append(self._sentence(chunk))
                chunk = []
        if chunk:
            sentences.append(self._sentence(chunk))
        if progress is not None:
            progress(1.0, "智能断句 1/1")
        return sentences

    @staticmethod
    def _result(item: AsrItem) -> AsrResult:
        """文本只由 item.key 决定：一次性跑与续跑必须得到同样的 token 流。"""
        text = "内容" + item.key
        tokens = [
            Token(text=ch, start_ms=item.start_ms + i * 120,
                  end_ms=item.start_ms + (i + 1) * 120, conf=0.9)
            for i, ch in enumerate(text)
        ]
        return AsrResult(key=item.key, text=text, tokens=tokens, confidence=0.9,
                         dur_s=(item.end_ms - item.start_ms) / 1000.0)

    @staticmethod
    def _sentence(tokens: list[Token]) -> dict:
        return {
            "text": "".join(t.text for t in tokens),
            "start_ms": tokens[0].start_ms,
            "end_ms": tokens[-1].end_ms,
            "confidence": 0.9,
            "tokens": list(tokens),
            "words": [{"text": t.text, "start_ms": t.start_ms, "end_ms": t.end_ms}
                      for t in tokens],
        }


# ------------------------------------------------------------------- fixture


class ResumeTestBase(unittest.TestCase):
    """每个用例一套 data/models/bin 目录 + 一个 Store。"""

    def setUp(self):
        self.tmp = scratch_dir("resume")
        self.bin_dir = self.tmp / "bin"
        self.models = self.tmp / "models"
        for key in ("aed", "vad", "punc"):
            (self.models / key).mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        for exe in ("ffmpeg", "ffprobe"):
            (self.bin_dir / (exe + ".exe")).write_bytes(b"stub")
        self.paths = Paths(data_dir=self.tmp / "data", models_dir=self.models,
                           ffmpeg_dir=self.bin_dir)
        self.paths.ensure()
        self.store = Store(self.paths.db_path)
        self._states: list = []

    def tearDown(self):
        for state in self._states:
            try:
                state.store.close()
            except Exception:  # noqa: BLE001 - 清理失败不该盖住断言结果
                pass
        try:
            self.store.close()
        finally:
            remove_scratch(self.tmp)

    # -- helpers -----------------------------------------------------
    def make_job(self, tag: str, store: Store | None = None) -> str:
        src = self.tmp / "media" / "{}-{}.mp4".format(tag, uuid.uuid4().hex[:8])
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"stub-media")
        target = store or self.store
        job = target.add_job(str(src), src.name, src.stat().st_size)
        return job["id"]

    def sidecar(self, job_id: str, paths: Paths | None = None) -> Path:
        return (paths or self.paths).data_dir / "partial" / "{}.json".format(job_id)

    def run_job(self, job_id: str, runtime: StubRuntime,
                settings: dict | None = None) -> dict | None:
        pipeline = Pipeline(self.paths, self.store,
                            dict(settings or DEFAULT_SETTINGS), runtime, emit=None)
        with patch_media():
            return pipeline.run(job_id, threading.Event())

    def run_expecting_failure(self, pipeline: Pipeline, job_id: str) -> BaseException:
        with patch_media():
            with self.assertRaises(BaseException) as ctx:
                pipeline.run(job_id, threading.Event())
        return ctx.exception

    def make_state(self, tag: str):
        """真 State（只用来测 retry/删除/Worker 的失败处理，不加载模型）。"""
        root = self.tmp / "state-{}".format(tag)
        data, models, bin_dir = root / "data", root / "models", root / "bin"
        for key in ("aed", "vad", "punc"):
            (models / key).mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        for exe in ("ffmpeg", "ffprobe"):
            (bin_dir / (exe + ".exe")).write_bytes(b"stub")
        args = engine_server.parse_args([
            "--token", "resume-token",
            "--data-dir", str(data),
            "--models-dir", str(models),
            "--ffmpeg", str(bin_dir),
        ])
        state = engine_server.State(args)
        self._states.append(state)
        return state


# --------------------------------------------------------------------- tests


class ResumeAfterFailureTest(ResumeTestBase):
    """B1：增量落盘 + 续跑，最终结果必须与一口气跑完完全一致。"""

    def test_failure_then_resume_matches_one_shot(self):
        settings = dict(DEFAULT_SETTINGS, batch_size=2)

        # 参照组：一口气跑完
        once_id = self.make_job("once")
        self.assertIsNotNone(self.run_job(once_id, StubRuntime(), settings))
        self.assertEqual(self.store.get_job(once_id)["status"], "done")
        self.assertFalse(self.sidecar(once_id).exists())

        # 对照组：第 2 批回调之后炸掉（模拟中途崩溃 / 断电）
        boom_id = self.make_job("boom")
        boom = StubRuntime(batch_fail_after=2, fail_with=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self.run_job(boom_id, boom, settings)
        self.assertEqual(self.store.get_job(boom_id)["status"], "error")

        sidecar = self.sidecar(boom_id)
        self.assertTrue(sidecar.is_file(), "失败后必须留下 sidecar 才能续跑")
        saved = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(saved["v"], 2)
        self.assertEqual(saved["media"]["duration_ms"], DURATION_MS)
        self.assertEqual(saved["media"]["size_bytes"],
                         (Path(self.store.get_job(boom_id)["path"]).stat().st_size))
        # 2 批 × 2 段：第 2 次回调的落盘已经完成，第 3 批没跑
        self.assertEqual([r["key"] for r in saved["results"]],
                         ["s0", "s1", "s2", "s3"])
        self.assertEqual(saved["results"][0]["tokens"][0]["text"], "内")
        self.assertIn("start_ms", saved["results"][0]["tokens"][0])

        # 续跑：新 Pipeline + 新桩，只应该识别缺的那一段
        resume = StubRuntime()
        self.assertIsNotNone(self.run_job(boom_id, resume, settings))
        self.assertEqual(resume.seen_keys, ["s4"], "已完成的段不允许重新识别")
        self.assertEqual(self.store.get_job(boom_id)["status"], "done")

        # 最终 transcript 与一次性跑完一致（只比句子，stats 含耗时必然不同）
        self.assertEqual(load_transcript(self.paths, boom_id)["sentences"],
                         load_transcript(self.paths, once_id)["sentences"])
        self.assertFalse(sidecar.exists(), "done 之后 sidecar 必须删掉")

    def test_media_mismatch_discards_sidecar(self):
        settings = dict(DEFAULT_SETTINGS, batch_size=2)
        job_id = self.make_job("mismatch")
        boom = StubRuntime(batch_fail_after=1, fail_with=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self.run_job(job_id, boom, settings)

        sidecar = self.sidecar(job_id)
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["media"]["duration_ms"] = DURATION_MS + 1234  # 用户换了个文件
        sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        again = StubRuntime()
        self.assertIsNotNone(self.run_job(job_id, again, settings))
        # 指纹不一致 → 丢弃 sidecar 从头跑：5 段全部重新识别
        self.assertEqual(again.seen_keys, ["s0", "s1", "s2", "s3", "s4"])
        self.assertEqual(self.store.get_job(job_id)["status"], "done")
        self.assertFalse(sidecar.exists())


class RetryAndDeleteTest(ResumeTestBase):
    """B1：retry 默认续跑、fresh 丢 sidecar；DELETE 连 sidecar 一起清。"""

    def test_fresh_retry_drops_sidecar(self):
        state = self.make_state("fresh")
        job_id = self.make_job("fresh", store=state.store)
        sidecar = self.sidecar(job_id, state.paths)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"v": 1, "media": {}, "results": []}),
                           encoding="utf-8")

        state.retry_job(job_id)  # 默认：保留 sidecar 续跑
        self.assertTrue(sidecar.is_file(), "默认 retry 必须保留 sidecar")
        self.assertEqual(state.store.get_job(job_id)["status"], "queued")

        state.retry_job(job_id, fresh=True)  # fresh：从头跑
        self.assertFalse(sidecar.is_file(), "fresh retry 必须删掉 sidecar")

    def test_delete_removes_sidecar(self):
        state = self.make_state("delete")
        job_id = self.make_job("delete", store=state.store)
        sidecar = self.sidecar(job_id, state.paths)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"v": 1, "media": {}, "results": []}),
                           encoding="utf-8")
        state.pipeline.batch_overrides[job_id] = 1

        state.remove_job(job_id)
        self.assertFalse(sidecar.exists(), "DELETE 必须一并删掉 sidecar")
        self.assertIsNone(state.store.get_job(job_id))
        self.assertNotIn(job_id, state.pipeline.batch_overrides)


class OomDowngradeTest(ResumeTestBase):
    """B2：OOM 自动降级重试一次，仍失败则报可执行的 oom 错误。"""

    def test_oom_downgrades_to_batch_one_once(self):
        state = self.make_state("oom")
        stub = StubRuntime(batch_fail_after=1, fail_with=oom_error())
        pipeline = Pipeline(state.paths, state.store, dict(DEFAULT_SETTINGS), stub)
        # Worker 只通过 state.pipeline 读批大小，这里换成桩 Pipeline
        state.pipeline = pipeline
        job_id = self.make_job("oom", store=state.store)
        worker = engine_server.Worker(state)  # 不起线程：只测失败处理

        first = self.run_expecting_failure(pipeline, job_id)
        self.assertTrue(is_out_of_memory(first))
        self.assertEqual(stub.seen_batches, [6], "第一次应使用设置里的批大小")
        row = state.store.get_job(job_id)
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["error"].startswith("oom:"), row["error"])
        self.assertIn("显存不足", row["error"])

        # 第一次失败 → 降级入队（且只入队一次）
        self.assertTrue(worker._schedule_oom_retry(row, first))
        self.assertEqual(state.pipeline.batch_overrides[job_id], 1)
        self.assertEqual(state.store.get_job(job_id)["status"], "queued")

        # 第二次：batch=1 仍然 OOM → 不再自动重试，报最终错误
        stub.seen_batches.clear()
        second = self.run_expecting_failure(pipeline, job_id)
        self.assertEqual(stub.seen_batches, [1], "降级后的批大小必须是 1")
        self.assertTrue(is_out_of_memory(second))
        self.assertFalse(worker._schedule_oom_retry(state.store.get_job(job_id), second))
        self.assertEqual(state.pipeline.batch_overrides[job_id], 1, "每个任务只自动重试一次")
        row = state.store.get_job(job_id)
        self.assertEqual(row["status"], "error")
        self.assertTrue(row["error"].startswith("oom:"), row["error"])
        self.assertIn("batch=1", row["error"])
        self.assertIn("续跑", row["error"])
        self.assertEqual(row["error"], "oom: " + OOM_MESSAGE)

    def test_oom_keeps_sidecar_for_resume(self):
        settings = dict(DEFAULT_SETTINGS, batch_size=2)
        job_id = self.make_job("oom-keep")
        stub = StubRuntime(batch_fail_after=1, fail_with=oom_error())
        with self.assertRaises(BaseException):
            self.run_job(job_id, stub, settings)
        sidecar = self.sidecar(job_id)
        self.assertTrue(sidecar.is_file(), "OOM 失败后 sidecar 必须保留")
        saved = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual([r["key"] for r in saved["results"]], ["s0", "s1"])


class RetryEndpointTest(ResumeTestBase):
    """B1：retry / delete 的 HTTP 契约（前端 retry 不带 body，必须能 200）。"""

    def test_retry_endpoint_accepts_bodyless_post_and_fresh(self):
        state = self.make_state("retry-http")
        app = engine_server.build_app(state)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False,
                                               log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                  name="vt-resume-http", daemon=True)
        thread.start()
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not server.started:
                if not thread.is_alive():
                    break
                time.sleep(0.02)
            self.assertTrue(server.started, "uvicorn 没起来")

            job_id = self.make_job("retry-http", store=state.store)
            sidecar = self.sidecar(job_id, state.paths)
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps({"v": 1, "media": {}, "results": []}),
                               encoding="utf-8")

            # 前端就是不传 body 的那一种调法
            status, text = self.request(port, state.token, "POST",
                                        "/api/jobs/{}/retry".format(job_id))
            self.assertEqual(status, 200, text)
            self.assertTrue(sidecar.is_file(), "默认 retry 必须保留 sidecar 续跑")
            self.assertEqual(state.store.get_job(job_id)["status"], "queued")

            status, text = self.request(port, state.token, "POST",
                                        "/api/jobs/{}/retry".format(job_id),
                                        {"fresh": True})
            self.assertEqual(status, 200, text)
            self.assertFalse(sidecar.is_file(), "fresh retry 必须删掉 sidecar")

            sidecar.write_text(json.dumps({"v": 1, "media": {}, "results": []}),
                               encoding="utf-8")
            status, text = self.request(port, state.token, "DELETE",
                                        "/api/jobs/{}".format(job_id))
            self.assertEqual(status, 200, text)
            self.assertFalse(sidecar.exists(), "DELETE 必须一并删掉 sidecar")
            self.assertIsNone(state.store.get_job(job_id))
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def request(port: int, token: str, method: str, path: str,
                body: dict | None = None) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        payload = None
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            return response.status, raw.decode("utf-8", "replace")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
