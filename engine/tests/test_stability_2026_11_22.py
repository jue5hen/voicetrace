"""2026-11-22 稳定性/性能审计修复的回归测试。

覆盖本轮改动：
* 排队取消的终态 stage 与 status 一致（SPEC §3.1）；
* 自动命名导出的 TOCTOU 原子占位（并发导出绝不静默覆盖）；
* DownloadManager / RuntimeManager 的 start 原子化与旧监视线程身份护栏；
* 引擎优雅退出时终止后台下载/安装子进程；
* 父进程看门狗的 ctypes 句柄原型（x64 不截断）；
* 长单句拆分的 O(n³) 性能修复（结果不变、缓存命中）；
* 无 id 损坏句不再把编辑/分析打成 500。

不加载任何模型：API 用例复用 test_server_api 的 stub 夹具。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from voicetrace_engine import server as engine_server
from voicetrace_engine.downloader import DownloadManager
from voicetrace_engine.runtime_manager import RuntimeManager
from voicetrace_engine.segmentation import (
    SEG_VERSION, _content_offsets, _word_boundaries, segment_sentence,
)
from voicetrace_engine.textutil import content_text, is_content, word_ends

from test_server_api import (
    EngineServerTest, make_transcript, remove_scratch, scratch_dir,
)


# ------------------------------------------------------------------ 取消终态


class QueuedCancelStageTest(EngineServerTest):
    def test_queued_cancel_stage_matches_status(self):
        job_id = self.make_job(1)
        status, data, _raw = self.request("POST", f"/api/jobs/{job_id}/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "canceled")
        # 旧实现写 stage="done"：按 stage 渲染会把已取消任务画成已完成。
        self.assertEqual(data["stage"], "canceled")
        row = self.state.store.get_job(job_id)
        self.assertEqual(row["stage"], "canceled")


# ------------------------------------------------------------------ 导出原子性


class ExclusiveExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch_dir("exclusive-export")

    def tearDown(self):
        remove_scratch(self.tmp)

    def test_exclusive_write_refuses_existing_and_keeps_original(self):
        path = self.tmp / "a.srt"
        path.write_text("original", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            engine_server.atomic_write_text(path, "new", exclusive=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_exclusive_write_creates_new_file(self):
        path = self.tmp / "b.srt"
        engine_server.atomic_write_text(path, "hello", exclusive=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "hello")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_plain_write_still_overwrites(self):
        path = self.tmp / "c.srt"
        path.write_text("old", encoding="utf-8")
        engine_server.atomic_write_text(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "new")

    def test_write_export_file_bumps_name_on_lost_race(self):
        path = self.tmp / "d.srt"
        path.write_text("taken", encoding="utf-8")
        out = engine_server.write_export_file(path, "mine", "utf-8", "d.srt")
        self.assertEqual(out.name, "d (2).srt")
        self.assertEqual(path.read_text(encoding="utf-8"), "taken")
        self.assertEqual(out.read_text(encoding="utf-8"), "mine")

    def test_concurrent_exports_never_clobber(self):
        # 核心 TOCTOU 回归：8 个并发导出抢同一个名字，必须落 8 个不同文件，
        # 且每个文件的内容都是自己的——旧实现 os.replace 会静默互覆。
        def worker(i: int) -> Path:
            return engine_server.write_export_file(
                self.tmp / "race.srt", f"content-{i}", "utf-8", "race.srt")

        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(worker, range(8)))
        self.assertEqual(len(set(paths)), 8)
        for i, path in enumerate(paths):
            self.assertEqual(path.read_text(encoding="utf-8"), f"content-{i}")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])


class ConcurrentExportApiTest(EngineServerTest):
    def test_parallel_default_exports_all_survive(self):
        job_id = self.make_job(2)

        def do_export(_i: int):
            return self.request("POST", f"/api/jobs/{job_id}/export",
                                body={"format": "srt"})

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(do_export, range(6)))
        paths = []
        for status, data, _raw in results:
            self.assertEqual(status, 200, data)
            self.assertFalse(data["fallback"])
            paths.append(data["path"])
        self.assertEqual(len(set(paths)), 6)
        for path in paths:
            text = Path(path).read_text(encoding="utf-8-sig")
            self.assertIn("-->", text)


# ------------------------------------------------------------------ 子进程管理


class FakeProc:
    def __init__(self, code=None):
        self._code = code
        self.pid = 4242
        self.terminated = False

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        self._code = -15


class DownloadManagerRaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch_dir("dl-race")
        self.models = self.tmp / "models"
        self.models.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()

    def tearDown(self):
        remove_scratch(self.tmp)

    def test_concurrent_start_spawns_one_child(self):
        mgr = DownloadManager(self.models, self.data)
        fake = FakeProc(code=None)
        calls = []

        def factory(*_a, **_k):
            calls.append(1)
            return fake

        with mock.patch("voicetrace_engine.downloader.subprocess.Popen",
                        side_effect=factory):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _i: mgr.start(), range(8)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(mgr._state, "running")
        fake._code = 0  # 让监视线程收尾
        if mgr._thread is not None:
            mgr._thread.join(timeout=5)

    def test_stale_monitor_cannot_overwrite_new_run(self):
        mgr = DownloadManager(self.models, self.data)
        old = FakeProc(code=0)      # 上一轮进程已退出
        new = FakeProc(code=None)   # 新一轮正在跑
        mgr._proc = new
        mgr._state = "running"
        mgr._repos = ["aed"]
        mgr._monitor(old, io.StringIO())  # 旧监视线程晚醒，同步走完
        self.assertEqual(mgr._state, "running")
        self.assertIs(mgr._proc, new)

    def test_monitor_reports_done_for_own_proc(self):
        mgr = DownloadManager(self.models, self.data)
        proc = FakeProc(code=0)
        mgr._proc = proc
        mgr._state = "running"
        mgr._monitor(proc, io.StringIO())
        self.assertEqual(mgr._state, "done")
        self.assertIsNone(mgr._error)

    def test_monitor_reports_canceled(self):
        mgr = DownloadManager(self.models, self.data)
        proc = FakeProc(code=-15)
        mgr._proc = proc
        mgr._state = "running"
        mgr._cancel_requested = True
        mgr._monitor(proc, io.StringIO())
        self.assertEqual(mgr._state, "canceled")


class RuntimeManagerRaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch_dir("rt-race")
        self.engine_root = self.tmp / "app" / "engine"
        self.engine_root.mkdir(parents=True)
        self.site = self.tmp / "app" / "runtime" / "site-packages"
        self.site.mkdir(parents=True)
        self.data = self.tmp / "data"
        self.data.mkdir()

    def tearDown(self):
        remove_scratch(self.tmp)

    def test_concurrent_start_spawns_one_child(self):
        mgr = RuntimeManager(self.engine_root, self.data)
        fake = FakeProc(code=None)
        calls = []

        def factory(*_a, **_k):
            calls.append(1)
            return fake

        with mock.patch.object(mgr, "_ready", return_value=False), \
                mock.patch("voicetrace_engine.runtime_manager.subprocess.Popen",
                           side_effect=factory):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _i: mgr.start(), range(8)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(mgr._state, "running")
        fake._code = 0
        if mgr._thread is not None:
            mgr._thread.join(timeout=5)

    def test_stale_monitor_cannot_overwrite_new_run(self):
        mgr = RuntimeManager(self.engine_root, self.data)
        old = FakeProc(code=0)
        new = FakeProc(code=None)
        mgr._proc = new
        mgr._state = "running"
        mgr._phase = "download"
        mgr._monitor(old, io.StringIO())
        self.assertEqual(mgr._state, "running")
        self.assertIs(mgr._proc, new)

    def test_monitor_reports_done_for_own_proc(self):
        mgr = RuntimeManager(self.engine_root, self.data)
        proc = FakeProc(code=0)
        mgr._proc = proc
        mgr._state = "running"
        with mock.patch.object(mgr, "_ready", return_value=True):
            mgr._monitor(proc, io.StringIO())
        self.assertEqual(mgr._state, "done")
        self.assertEqual(mgr._phase, "done")


# ------------------------------------------------------------------ 退出清理


class ShutdownCleanupTest(unittest.TestCase):
    def test_shutdown_cancels_background_downloads(self):
        calls = []

        class Manager:
            def cancel(self):
                calls.append(1)

        class Worker:
            stop = threading.Event()

            def is_alive(self):
                return False

        class Pipeline:
            def cancel_all(self):
                pass

        class Store:
            def checkpoint(self):
                pass

            def close(self):
                pass

        state = types.SimpleNamespace(
            worker=Worker(), pipeline=Pipeline(), store=Store(),
            models=Manager(), runtime_setup=Manager(),
        )
        engine_server.shutdown(state, None)
        self.assertEqual(len(calls), 2)
        self.assertTrue(state.worker.stop.is_set())


# ------------------------------------------------------------------ 看门狗


@unittest.skipUnless(os.name == "nt", "父进程看门狗仅 Windows")
class WatchParentTest(unittest.TestCase):
    def test_invalid_pid_returns_cleanly(self):
        # 不存在的 pid：OpenProcess 失败 → 记警告后返回，绝不抛异常。
        engine_server._watch_parent(0xFFFFFFFF)

    def test_parent_exit_requests_graceful_shutdown(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.4)"])
        server = types.SimpleNamespace(should_exit=False)
        thread = threading.Thread(
            target=engine_server._watch_parent, args=(proc.pid, server),
            daemon=True)
        thread.start()
        proc.wait(timeout=15)
        thread.join(timeout=10)
        self.assertTrue(server.should_exit)


# ------------------------------------------------------------------ 拆分性能


class SegmentationPerfTest(unittest.TestCase):
    def test_word_boundaries_matches_reference_and_caches(self):
        text = "我们今天去公园散步，然后回家吃饭。"
        got = _word_boundaries(text)
        ends = word_ends(text)
        total = len(content_text(text))
        ref = set()
        count = 0
        for i, ch in enumerate(text):
            if is_content(ch):
                count += 1
            if i + 1 in ends and 0 < count < total:
                ref.add(count)
        self.assertEqual(set(got), ref)
        before = _word_boundaries.cache_info().hits
        _word_boundaries(text)
        self.assertGreater(_word_boundaries.cache_info().hits, before)

    def test_content_offsets_returns_fresh_list(self):
        text = "a，b。c"
        first = _content_offsets(text)
        second = _content_offsets(text)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first.append(999)
        self.assertNotIn(999, _content_offsets(text))

    def test_long_sentence_split_is_fast_and_lossless(self):
        # ~250 字无标点长句：旧实现每个候选切点都 O(n²) 重扫全文，
        # 整体退化到秒级；修复后应在毫秒级完成且内容一字不丢。
        chars = "这是一段很长的没有标点的口语文本用来验证长句拆分的性能与正确性" * 8
        n = len(chars)
        words = [{"text": ch, "start_ms": i * 120, "end_ms": (i + 1) * 120}
                 for i, ch in enumerate(chars)]
        sentence = {"id": 0, "text": chars, "start_ms": 0, "end_ms": n * 120,
                    "words": words, "conf": 0.9}
        started = time.perf_counter()
        pieces = segment_sentence(sentence, max_chars=22, max_ms=6500)
        elapsed = time.perf_counter() - started
        joined = "".join(content_text(p["text"]) for p in pieces)
        self.assertEqual(joined, chars)
        self.assertGreaterEqual(len(pieces), (n + 21) // 22)
        for piece in pieces:
            self.assertLessEqual(len(content_text(piece["text"])), 22)
        self.assertLess(elapsed, 3.0, f"长句拆分耗时 {elapsed:.2f}s，疑似性能回归")


# ------------------------------------------------------------------ 损坏句容错


class MalformedSentenceTest(EngineServerTest):
    def test_sentence_without_id_does_not_break_edit(self):
        job_id = self.make_job(1)
        transcript = make_transcript(1)
        transcript["job_id"] = job_id
        transcript["seg_version"] = SEG_VERSION
        # 损坏句放在目标句前面：旧实现 next(s["id"]...) 先撞上它就 KeyError。
        transcript["sentences"].insert(0, {
            "text": "没有 id 的损坏句", "start_ms": 0, "end_ms": 500,
        })
        path = self.state.transcript_path(job_id)
        path.write_text(
            __import__("json").dumps(transcript, ensure_ascii=False),
            encoding="utf-8")
        status, data, _raw = self.request(
            "PUT", f"/api/jobs/{job_id}/sentences/0", body={"text": "改过的句子。"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["transcript"]["sentences"][-1]["text"], "改过的句子。")
        # 分析也不被损坏句拖垮
        status, data, _raw = self.request("GET", f"/api/jobs/{job_id}/issues")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()