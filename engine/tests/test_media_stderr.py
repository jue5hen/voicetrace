"""Regression tests: ffmpeg must never deadlock on an undrained stderr pipe.

The 2026-11-17 audit found that extract_audio() only consumed stdout while
leaving stderr to be read after the process exited. ffmpeg errors easily exceed
the 4 KB pipe buffer, so the single worker thread blocked forever and
cancellation could not help. These tests drive extract_audio() with a fake
ffmpeg (a .cmd shim) that floods stderr, hangs, or must be cancelled.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from voicetrace_engine import media

WORKER = '''import pathlib
import sys
import time

here = pathlib.Path(__file__).resolve().parent
mode = (here / "mode.txt").read_text(encoding="utf-8").strip()
NL = chr(10)
if mode == "spam":
    for _ in range(40):
        sys.stdout.write("progress=continue" + NL)
        sys.stdout.flush()
        sys.stderr.write("x" * 8192 + NL)
        sys.stderr.flush()
        time.sleep(0.01)
    sys.stderr.write("conversion failed for testing" + NL)
    sys.stderr.flush()
    sys.exit(1)
if mode == "hang":
    time.sleep(600)
sys.exit(0)
'''


@unittest.skipUnless(os.name == "nt", "uses a .cmd shim")
class ExtractAudioStderrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "worker.py").write_text(WORKER, encoding="utf-8")
        (self.dir / "mode.txt").write_text("spam", encoding="utf-8")
        shim = self.dir / "fake_ffmpeg.cmd"
        body = '@echo off' + chr(13) + chr(10)
        body += '"' + sys.executable + '" "' + str(self.dir / "worker.py") + '" %*' + chr(13) + chr(10)
        shim.write_text(body, encoding="utf-8")
        self.ffmpeg = shim
        self.src = self.dir / "in.mp4"
        self.src.write_bytes(b"not a real video")
        self.dst = self.dir / "out.wav"

    def tearDown(self):
        self.tmp.cleanup()

    def _set_mode(self, mode: str) -> None:
        (self.dir / "mode.txt").write_text(mode, encoding="utf-8")

    def test_large_stderr_does_not_deadlock(self):
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            media.extract_audio(self.ffmpeg, self.src, self.dst, 1000, stall_timeout_s=30)
        self.assertLess(time.monotonic() - started, 40, "must not hang on stderr")

    def test_stalled_child_is_killed_by_watchdog(self):
        self._set_mode("hang")
        started = time.monotonic()
        with self.assertRaises(RuntimeError) as ctx:
            media.extract_audio(self.ffmpeg, self.src, self.dst, 1000, stall_timeout_s=1)
        self.assertIn("stalled", str(ctx.exception))
        self.assertLess(time.monotonic() - started, 30)

    def test_cancel_raises_cancelled_not_error(self):
        self._set_mode("hang")
        from voicetrace_engine.runtime import Cancelled

        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        with self.assertRaises(Cancelled):
            media.extract_audio(self.ffmpeg, self.src, self.dst, 1000, cancel=cancel)
        self.assertLess(time.monotonic() - started, 30)


if __name__ == "__main__":
    unittest.main()
