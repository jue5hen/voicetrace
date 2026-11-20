"""ASR 自适应批处理的回归测试（2026-11-17 长视频稳定性）。

用户要通宵排队跑 1–2 小时素材：撞一次 OOM 不能整条白跑。这里用桩对象钉住
两方共享契约，全程不 import torch、不碰 CUDA：

* 批太大就 OOM 时必须对半拆开重试，直到每批都能过；
* 拆到单段仍 OOM 时抛 AsrOutOfMemory(batch_size=1)，交给上层降级；
* on_batch 每批回调一次，回调自己抛异常不能影响识别结果；
* 批前按显存余量主动缩批，缩过之后本任务只降不升。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from voicetrace_engine.config import (_SETTING_LIMITS, DEFAULT_SETTINGS, Paths,
                                      _coerce)
from voicetrace_engine.runtime import AsrItem, AsrOutOfMemory, Cancelled, Runtime

SAMPLE_RATE = 16000
MB = 1024 * 1024


# --------------------------------------------------------------------- 桩对象


def make_items(count: int = 10) -> list[AsrItem]:
    """造 count 段等长的假语音；识别桩不读音频内容，零数组足够。"""
    items: list[AsrItem] = []
    for i in range(count):
        start_ms = i * 2000
        items.append(AsrItem(
            key=f"s{i}",
            start_ms=start_ms,
            end_ms=start_ms + 1000,
            audio=np.zeros(SAMPLE_RATE // 10, dtype=np.int16),
        ))
    return items


class FakeAsr:
    """按"最多一次吃几段"模拟 CUDA OOM 的识别模型桩。"""

    def __init__(self, max_ok: int) -> None:
        self.max_ok = max_ok
        self.max_success = 0          # 成功过的最大批大小
        self.calls: list[int] = []    # 每次调用的批大小，便于断言拆分路径

    def transcribe(self, keys: list[str],
                   audios: list[tuple[int, np.ndarray]]) -> list[dict]:
        self.calls.append(len(keys))
        if len(keys) > self.max_ok:
            raise RuntimeError("CUDA out of memory. Tried to allocate 512.00 MiB")
        self.max_success = max(self.max_success, len(keys))
        return [{
            "uttid": key,
            "text": "测试",
            "timestamp": [["测", 0.10, 0.30], ["试", 0.30, 0.50]],
            "confidence": 0.9,
            "dur_s": round(len(audios[i][1]) / SAMPLE_RATE, 3),
        } for i, key in enumerate(keys)]


class FakeCuda:
    """只实现 runtime 会用到的那几个 CUDA 接口，绝不初始化真显卡。"""

    def __init__(self, free_bytes: int, allocated: int = 0) -> None:
        self.free_bytes = free_bytes
        self.allocated = allocated
        self.peak = allocated
        self.emptied = 0
        self.fraction: float | None = None

    def is_available(self) -> bool:
        return True

    def mem_get_info(self) -> tuple[int, int]:
        return self.free_bytes, self.free_bytes + self.allocated

    def max_memory_allocated(self) -> int:
        return self.peak

    def empty_cache(self) -> None:
        self.emptied += 1

    def set_per_process_memory_fraction(self, fraction: float) -> None:
        self.fraction = fraction


class FakeTorch:
    """torch 模块的最小替身；只有一个 cuda 命名空间。"""

    def __init__(self, cuda: FakeCuda) -> None:
        self.cuda = cuda


def make_runtime(asr, settings: dict | None = None, torch=None,
                 device: str = "cpu") -> Runtime:
    """手工装配 Runtime：不走 load()，因此永远不 import torch。"""
    paths = Paths(data_dir=Path("."), models_dir=Path("."))
    runtime = Runtime(paths, dict(settings or {}), use_gpu=device == "cuda")
    runtime._asr = asr
    runtime._torch = torch
    runtime.device = device
    return runtime


# ----------------------------------------------------------- A2 自适应批处理


class AdaptiveBatchTests(unittest.TestCase):
    """A2：OOM 自愈、缩批记忆与 on_batch 契约。"""

    def test_splits_until_batch_fits_and_keeps_every_segment(self):
        asr = FakeAsr(max_ok=2)
        runtime = make_runtime(asr)
        seen: list[list[str]] = []
        prog: list[tuple[float, str]] = []
        results = runtime.transcribe(
            make_items(10), batch_size=6,
            progress=lambda p, m: prog.append((p, m)),
            on_batch=lambda batch: seen.append([r.key for r in batch]),
        )
        # 1) 10 段全回来，key 一个不少、顺序不乱
        self.assertEqual([r.key for r in results], [f"s{i}" for i in range(10)])
        self.assertEqual(sorted(r.key for r in results), [f"s{i}" for i in range(10)])
        # 2) 6 段的批必须一路拆到 2 段以内才过得去
        self.assertLessEqual(asr.max_success, 2)
        self.assertEqual(asr.max_success, 2)
        # 3) 每批回调一次，累计条数等于全部素材
        self.assertGreater(len(seen), 1)
        self.assertEqual(sum(len(b) for b in seen), 10)
        self.assertEqual(sorted(k for b in seen for k in b), [f"s{i}" for i in range(10)])
        # 进度语义不变：全部段识别完时进度回收到 1.0
        self.assertTrue(prog)
        self.assertAlmostEqual(prog[-1][0], 1.0)
        self.assertIn("识别", prog[-1][1])

    def test_always_oom_raises_asr_out_of_memory(self):
        asr = FakeAsr(max_ok=0)
        runtime = make_runtime(asr)
        with self.assertRaises(AsrOutOfMemory) as ctx:
            runtime.transcribe(make_items(10), batch_size=6)
        self.assertEqual(ctx.exception.batch_size, 1)
        self.assertIsInstance(ctx.exception, RuntimeError)
        # 拆到单段才放弃：最后一次调用必须是 1 段
        self.assertEqual(asr.calls[-1], 1)

    def test_on_batch_exception_is_swallowed(self):
        asr = FakeAsr(max_ok=10)
        runtime = make_runtime(asr)
        calls: list[int] = []

        def boom(batch) -> None:
            calls.append(len(batch))
            raise ValueError("上游落盘失败")

        with self.assertLogs("voicetrace.asr", level="WARNING") as logs:
            results = runtime.transcribe(make_items(10), batch_size=6, on_batch=boom)
        self.assertEqual(len(results), 10)
        self.assertEqual(sum(calls), 10)
        self.assertTrue(any("on_batch" in line for line in logs.output), logs.output)

    def test_shrinks_before_decoding_when_vram_is_tight(self):
        asr = FakeAsr(max_ok=99)
        cuda = FakeCuda(free_bytes=200 * MB)
        runtime = make_runtime(asr, torch=FakeTorch(cuda), device="cuda")
        results = runtime.transcribe(make_items(10), batch_size=6)
        self.assertEqual(len(results), 10)
        # 200MB 余量对 220MB/段的保守估算只够单段，缩过之后本任务不回升
        self.assertEqual(asr.max_success, 1)
        self.assertEqual(runtime.effective_batch_size, 1)

    def test_effective_batch_size_never_rises(self):
        asr = FakeAsr(max_ok=2)
        runtime = make_runtime(asr)
        runtime.transcribe(make_items(6), batch_size=6)
        shrunk = runtime.effective_batch_size
        self.assertIsInstance(shrunk, int)
        self.assertLess(shrunk, 6)
        # 第二次调用仍从缩小的批起步（保守，不自动回升）
        runtime.transcribe(make_items(4), batch_size=6)
        self.assertEqual(runtime.effective_batch_size, shrunk)

    def test_cancel_is_honoured_between_retries(self):
        asr = FakeAsr(max_ok=2)
        runtime = make_runtime(asr)
        calls = {"n": 0}

        def cancelled() -> bool:
            calls["n"] += 1
            return calls["n"] > 1   # 第一次放行，撞 OOM 拆分后就要求取消

        with self.assertRaises(Cancelled):
            runtime.transcribe(make_items(10), batch_size=6, cancelled=cancelled)
        # 只跑了一批（撞 OOM）就被取消，没有再往下拆
        self.assertEqual(len(asr.calls), 1)

    def test_cpu_runs_never_probe_vram(self):
        runtime = make_runtime(FakeAsr(max_ok=10))
        self.assertIsNone(runtime._torch)
        self.assertIsNone(runtime._vram_state())
        self.assertEqual(len(runtime.transcribe(make_items(4), batch_size=6)), 4)


# --------------------------------------------------------------- A1 显存上限


class VramLimitTests(unittest.TestCase):
    """A1：显存硬上限；配置非法只记日志，绝不能让加载失败。"""

    def test_fraction_is_clamped_to_safe_range(self):
        cuda = FakeCuda(free_bytes=1 * MB)
        runtime = make_runtime(FakeAsr(1), settings={"gpu_memory_fraction": 1.5},
                               torch=FakeTorch(cuda), device="cuda")
        runtime._apply_vram_limit(runtime._torch)
        self.assertAlmostEqual(cuda.fraction, 0.95)

        cuda.fraction = None
        runtime.settings["gpu_memory_fraction"] = 0.1
        runtime._apply_vram_limit(runtime._torch)
        self.assertAlmostEqual(cuda.fraction, 0.5)

    def test_bad_fraction_is_logged_and_skipped(self):
        cuda = FakeCuda(free_bytes=1 * MB)
        runtime = make_runtime(FakeAsr(1), settings={"gpu_memory_fraction": "abc"},
                               torch=FakeTorch(cuda), device="cuda")
        with self.assertLogs("voicetrace.asr", level="WARNING"):
            runtime._apply_vram_limit(runtime._torch)
        self.assertIsNone(cuda.fraction)

    def test_headroom_setting_is_clamped(self):
        runtime = make_runtime(FakeAsr(1), settings={"vram_headroom_mb": 5})
        self.assertEqual(runtime._headroom_bytes(), 200 * MB)
        runtime.settings["vram_headroom_mb"] = "abc"
        with self.assertLogs("voicetrace.asr", level="WARNING"):
            self.assertEqual(runtime._headroom_bytes(), 700 * MB)


class AllocConfEnvTests(unittest.TestCase):
    """A1：expandable_segments 必须在 import torch 之前设，且不覆盖用户设置。"""

    def test_sets_expandable_segments_when_absent(self):
        runtime = make_runtime(FakeAsr(1))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYTORCH_CUDA_ALLOC_CONF", None)
            runtime._set_alloc_conf()
            self.assertEqual(os.environ.get("PYTORCH_CUDA_ALLOC_CONF"),
                             "expandable_segments:True")
            runtime._set_alloc_conf()  # 幂等
            self.assertEqual(os.environ.get("PYTORCH_CUDA_ALLOC_CONF"),
                             "expandable_segments:True")

    def test_keeps_user_configuration(self):
        runtime = make_runtime(FakeAsr(1))
        with mock.patch.dict(os.environ,
                             {"PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:64"}):
            runtime._set_alloc_conf()
            self.assertEqual(os.environ["PYTORCH_CUDA_ALLOC_CONF"], "max_split_size_mb:64")


# ------------------------------------------------------------------- 设置项


class GpuSettingsTests(unittest.TestCase):
    """新增的两个设置项：默认值与区间。"""

    def test_defaults(self):
        self.assertAlmostEqual(DEFAULT_SETTINGS["gpu_memory_fraction"], 0.88)
        self.assertEqual(DEFAULT_SETTINGS["vram_headroom_mb"], 700)

    def test_limits_are_enforced(self):
        self.assertEqual(_SETTING_LIMITS["vram_headroom_mb"], (200, 4000))
        self.assertAlmostEqual(_coerce("gpu_memory_fraction", 2.0, 0.88), 0.95)
        self.assertAlmostEqual(_coerce("gpu_memory_fraction", 0.1, 0.88), 0.5)
        self.assertAlmostEqual(_coerce("gpu_memory_fraction", "abc", 0.88), 0.88)
        self.assertEqual(_coerce("vram_headroom_mb", 99999, 700), 4000)
        self.assertEqual(_coerce("vram_headroom_mb", "x", 700), 700)


# --------------------------------------------- 头部余量语义（下限，不是加数）


class _PlateauAsr(FakeAsr):
    """模拟长视频显存平台期：第一批之后空闲骤降到平台值，峰值每批再涨一点。"""

    def __init__(self, cuda: "FakeCuda", free_after: int, peak_step: int) -> None:
        super().__init__(max_ok=99)
        self.cuda = cuda
        self.free_after = free_after
        self.peak_step = peak_step

    def transcribe(self, keys, audios):
        out = super().transcribe(keys, audios)
        self.cuda.free_bytes = self.free_after
        self.cuda.peak += self.peak_step
        return out


class HeadroomFloorTests(unittest.TestCase):
    """回归：头部余量曾用加法（需求×1.3 + 头部），平台期会把批级联缩到 1。"""

    def test_plateau_does_not_cascade_to_single_segment(self):
        # 首批空闲 5000MB（保守估算放得下）；之后进入平台期：空闲 900MB、峰值每批 +200MB。
        # 加法规则会算成 900 < 260 + 700 = 960 而误缩；下限规则 max(260, 700) = 700 → 继续按 6 编批。
        cuda = FakeCuda(free_bytes=5000 * MB)
        asr = _PlateauAsr(cuda, free_after=900 * MB, peak_step=200 * MB)
        runtime = make_runtime(asr, torch=FakeTorch(cuda), device="cuda")
        results = runtime.transcribe(make_items(12), batch_size=6)
        self.assertEqual(len(results), 12)
        self.assertEqual(asr.calls, [6, 6], "平台期不应缩批")
        self.assertEqual(runtime.effective_batch_size, 6)

    def test_cascades_only_when_free_is_below_the_floor(self):
        # 空闲 600MB < 头部下限 700MB：余量确实不够，按最小批跑（这是有意的保守行为）
        cuda = FakeCuda(free_bytes=600 * MB)
        asr = FakeAsr(max_ok=99)
        runtime = make_runtime(asr, torch=FakeTorch(cuda), device="cuda")
        runtime.transcribe(make_items(8), batch_size=6)
        self.assertEqual(runtime.effective_batch_size, 1)
        self.assertTrue(all(size == 1 for size in asr.calls), asr.calls)

    def test_needs_side_still_shrinks_when_batch_is_too_big(self):
        # 空闲 800MB、保守估算 220MB/段：需求侧必须缩批（6 段要 1716MB 放不下）
        cuda = FakeCuda(free_bytes=800 * MB)
        asr = FakeAsr(max_ok=99)
        runtime = make_runtime(asr, torch=FakeTorch(cuda), device="cuda")
        results = runtime.transcribe(make_items(6), batch_size=6)
        self.assertEqual(len(results), 6)
        self.assertLess(max(asr.calls), 6, f"应当缩批，实际 {asr.calls}")
        self.assertGreaterEqual(runtime.effective_batch_size, 1)


# --------------------------------------------------- 跨调用的降级请求必须生效


class RequestedBatchOverrideTests(unittest.TestCase):
    """回归：effective_batch_size 跨任务粘住会吃掉「强制 batch=1」。

    OOM 降级时 pipeline 会传 batch_size=1；如果上一任务把 effective 缩到 6，
    旧实现会继续按 6 编批（实测 [6] 而不是 [1,1,1,1,1,1]），降级形同虚设。
    """

    def test_lower_request_takes_effect_after_a_shrinking_run(self):
        asr = FakeAsr(max_ok=1)          # 只有单段能过，逼它一路缩到 1
        runtime = make_runtime(asr)
        runtime.transcribe(make_items(4), batch_size=6)
        self.assertEqual(runtime.effective_batch_size, 1)

        asr.calls.clear()
        runtime.transcribe(make_items(3), batch_size=1)
        self.assertEqual(asr.calls, [1, 1, 1], "强制 batch=1 必须生效")

    def test_higher_request_does_not_grow_back(self):
        """保守语义：缩小之后，更大的请求不会让它自动回升（需重载引擎）。"""
        asr = FakeAsr(max_ok=1)
        runtime = make_runtime(asr)
        runtime.transcribe(make_items(3), batch_size=6)
        self.assertEqual(runtime.effective_batch_size, 1)

        asr.calls.clear()
        runtime.transcribe(make_items(4), batch_size=6)
        self.assertTrue(all(size == 1 for size in asr.calls),
                        f"不应自行回升，实际 {asr.calls}")


if __name__ == "__main__":
    unittest.main()
