"""FireRedASR2-AED / FireRedVAD / FireRedPunc runtime wrappers.

The models are loaded lazily and kept resident.  Everything here is
process-local: the engine runs as a hidden child process of the Tauri shell.

This module also implements the "audio -> sentences with per-character
timings" part of the pipeline.  The design goal is bounded memory for
arbitrarily long videos:

  * the 16 kHz mono wav is memory-mapped, never copied whole;
  * audio is cut into windows (default 30 min) with 10 s of context on each
    side, VAD runs on the window, and only segments whose *start* lies in the
    window core are kept, so no speech is duplicated or lost at boundaries;
  * ASR runs on length-bucketed batches (AED accepts up to 60 s per segment);
  * punctuation runs on ~300-character chunks, then sentences are merged on
    weak punctuation so a sentence is a real sentence, not a comma clause.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import numpy as np

from .config import Paths

log = logging.getLogger("voicetrace.asr")

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]

TERMINAL = set("。！？!?…")
WEAK = set("，、,;；:：")
PUNCT = TERMINAL | WEAK

_MB = 1024 * 1024
# 首批准入用的保守估算：8GB 卡实测单段约占 200MB 显存。
_SEGMENT_VRAM_BYTES = 220 * _MB
# 每批前保留的显存余量（设置项 vram_headroom_mb 覆盖）。
_HEADROOM_DEFAULT_MB = 700
_HEADROOM_MIN_MB = 200
_HEADROOM_MAX_MB = 4000
# CUDA 单进程显存上限的默认值与允许区间（整卡比例）。
_GPU_FRACTION_DEFAULT = 0.88
_GPU_FRACTION_MIN = 0.5
_GPU_FRACTION_MAX = 0.95


class EngineNotReady(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class AsrOutOfMemory(RuntimeError):
    """批已缩到 1 段仍然 OOM：上层据此降级重试或提示用户。"""

    def __init__(self, batch_size: int = 1, message: str | None = None) -> None:
        super().__init__(message or "CUDA 显存不足：单段识别仍然 out of memory")
        # 上层要按这个值判断还能不能再降级，所以必须带出来
        self.batch_size = int(batch_size)


def _is_content(ch: str) -> bool:
    return not (ch.isspace() or ch in PUNCT or ch in "“”\"'‘’（）()《》【】[]—…·")


def content_only(s: str) -> str:
    return "".join(ch for ch in s if _is_content(ch))


@dataclass
class Token:
    text: str
    start_ms: int
    end_ms: int
    conf: float = 1.0

    def as_tuple(self) -> tuple[str, float, float]:
        return (self.text, self.start_ms / 1000.0, self.end_ms / 1000.0)


@dataclass
class AsrItem:
    key: str
    start_ms: int
    end_ms: int
    audio: np.ndarray


@dataclass
class AsrResult:
    key: str
    text: str
    tokens: list[Token] = field(default_factory=list)
    confidence: float = 0.0
    dur_s: float = 0.0
    # Boundary of the ASR item this result came from.  The sidecar stores it so
    # a resumed run can prove the cached result still matches the segment
    # (same key is not enough when VAD/segmentation changed).
    start_ms: int = 0
    end_ms: int = 0


class Runtime:
    """Holds the three models and exposes the calls the pipeline needs."""

    def __init__(self, paths: Paths, settings: dict, use_gpu: bool = True):
        self.paths = paths
        self.settings = settings
        self.use_gpu = use_gpu
        self.device = "cpu"
        self.ready = False
        self.error: str | None = None
        self.loading = False
        self._lock = threading.RLock()
        self._asr = None
        self._vad = None
        self._punc = None
        self._torch = None
        self._loaded_modules = False
        # 自适应批处理：本任务内一旦缩过批就记在这里，load() 时清零。
        self.effective_batch_size: int | None = None
        # 显存探测失败后置位，避免每批刷同一条 warning。
        self._vram_probe_broken = False

    # ------------------------------------------------------------------ load

    def _import(self):
        if self._loaded_modules:
            return
        # 必须在 import torch 之前设：分配器在首次 CUDA 分配时才读这个变量
        self._set_alloc_conf()
        root = str(self.paths.third_party())
        if root not in sys.path:
            sys.path.insert(0, root)
        import torch  # noqa: F401
        from fireredasr2s.fireredasr2 import FireRedAsr2, FireRedAsr2Config  # noqa: F401
        from fireredasr2s.fireredpunc import FireRedPunc, FireRedPuncConfig  # noqa: F401
        from fireredasr2s.fireredvad import FireRedVad, FireRedVadConfig  # noqa: F401
        self._torch = torch
        self._loaded_modules = True

    def _set_alloc_conf(self) -> None:
        """在 import torch 之前打开可扩展显存段。

        长素材跑到 60 分钟以后，缓存块碎片化会让"其实还有空闲块"的请求
        报 OOM；expandable_segments 正是为这个场景准备的。
        用户若自己设了同名变量就尊重他的配置，不覆盖。
        """
        if os.environ.get("PYTORCH_CUDA_ALLOC_CONF"):
            return
        os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        log.info("设置 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True")

    def _apply_vram_limit(self, torch) -> None:
        """把本进程的 CUDA 显存封顶在整卡的 gpu_memory_fraction。

        目的：宁可我们这边确定性 OOM（随后缩批重试），也不要把整机显存
        吃满拖到桌面卡顿。设置读不出来只记日志，绝不能让加载失败。
        """
        try:
            fraction = float(self.settings.get("gpu_memory_fraction", _GPU_FRACTION_DEFAULT))
        except (TypeError, ValueError):
            # 配置坏了就退回"不设限"：识别能不能跑比上限重要
            log.warning("gpu_memory_fraction 配置非法，跳过本次显存上限设置")
            return
        fraction = max(_GPU_FRACTION_MIN, min(_GPU_FRACTION_MAX, fraction))
        try:
            torch.cuda.set_per_process_memory_fraction(fraction)
            log.info("CUDA 单进程显存上限：整卡的 %.0f%%", fraction * 100)
        except Exception:  # noqa: BLE001 - 老驱动/torch 不支持时必须继续跑
            log.warning("set_per_process_memory_fraction(%.2f) 失败，继续运行", fraction,
                        exc_info=True)

    def load(self, progress: ProgressFn | None = None) -> None:
        with self._lock:
            if self.ready or self.loading:
                return
            self.loading = True
            # 重新加载即新任务：上一次缩过的批不该继续约束这一次
            self.effective_batch_size = None
            self._vram_probe_broken = False
            try:
                self._import()
                torch = self._torch
                has_cuda = bool(torch.cuda.is_available())
                requested = str(self.settings.get("device", "auto") or "auto").lower()
                if requested == "cpu":
                    gpu = False
                elif requested == "cuda":
                    gpu = bool(self.use_gpu and has_cuda)
                    if not gpu:
                        log.warning("设置 device=cuda，但 --no-gpu 或 CUDA 不可用，回退 CPU")
                else:
                    gpu = bool(self.use_gpu and has_cuda)
                self.device = "cuda" if gpu else "cpu"
                log.info("模型设备：%s（设置 device=%s）", self.device, requested)
                if gpu:
                    # 必须在加载模型之前设上限，否则模型已经把余量吃掉了
                    self._apply_vram_limit(torch)
                report = progress or (lambda f, m: None)
                report(0.02, "加载语音活动检测模型")
                from fireredasr2s.fireredvad import FireRedVad, FireRedVadConfig

                vad_dir = str(self.paths.model("vad"))
                # the HF repo mirrors several checkpoints: VAD/ is the
                # general-purpose 16 kHz speech detector we want.
                cand = self.paths.model("vad") / "VAD"
                if (cand / "cmvn.ark").is_file():
                    vad_dir = str(cand)
                self._vad = FireRedVad.from_pretrained(
                    vad_dir,
                    FireRedVadConfig(
                        use_gpu=False,  # tiny model, GPU transfers cost more
                        speech_threshold=0.4,
                        min_speech_frame=20,
                        max_speech_frame=2000,
                        min_silence_frame=20,
                        merge_silence_frame=12,
                        extend_speech_frame=10,
                        chunk_max_frame=15000,
                    ),
                )
                report(0.10, "加载标点模型")
                from fireredasr2s.fireredpunc import FireRedPunc, FireRedPuncConfig

                self._punc = FireRedPunc.from_pretrained(
                    str(self.paths.model("punc")), FireRedPuncConfig(use_gpu=gpu)
                )
                report(0.18, "加载语音识别模型（约 4.7 GB，首次较慢）")
                from fireredasr2s.fireredasr2 import FireRedAsr2, FireRedAsr2Config

                cfg = FireRedAsr2Config(
                    use_gpu=gpu,
                    use_half=bool(self.settings.get("half", True)) and gpu,
                    beam_size=int(self.settings.get("beam_size", 3)),
                    nbest=1,
                    return_timestamp=True,
                )
                self._asr = FireRedAsr2.from_pretrained(
                    "aed", str(self.paths.model("aed")), cfg
                )
                self.ready = True
                self.error = None
                report(1.0, "引擎就绪")
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
                log.exception("model load failed")
                # Roll back every half-loaded reference: a failed AED load used
                # to leave vad/punc alive, and the retry's factory observed
                # stale instances.
                self._asr = self._vad = self._punc = None
                self.ready = False
                self.device = "cpu"
                try:
                    if self._torch is not None and self._torch.cuda.is_available():
                        self._torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
                raise
            finally:
                self.loading = False

    def unload(self) -> None:
        with self._lock:
            self._asr = self._vad = self._punc = None
            self.ready = False
            self.device = "cpu"
            try:
                if self._torch is not None and self._torch.cuda.is_available():
                    self._torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ vram

    def _vram_state(self) -> tuple[int, int] | None:
        """返回（空闲字节, 已分配峰值字节）；不知道就返回 None。

        返回 None 时调用方必须整段跳过主动缩批——探测不到就按原计划跑，
        不能让"防止 OOM"自己变成新的失败点。探测失败只记一次日志免刷屏。
        """
        torch = self._torch
        if torch is None or self.device != "cuda" or self._vram_probe_broken:
            return None
        try:
            if not bool(torch.cuda.is_available()):
                return None
            free, _total = torch.cuda.mem_get_info()
            return int(free), int(torch.cuda.max_memory_allocated())
        except Exception:  # noqa: BLE001 - 探测失败只能降级为"不做预防"
            self._vram_probe_broken = True
            log.warning("显存探测失败，本任务跳过主动缩批", exc_info=True)
            return None

    def _empty_cache(self) -> None:
        """OOM 后立刻归还缓存块，给拆小后的重试腾地方。"""
        torch = self._torch
        if torch is None:
            return
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - 清理失败不影响重试路径本身
            log.warning("torch.cuda.empty_cache() 失败，继续重试", exc_info=True)

    def _headroom_bytes(self) -> int:
        """每批前要留出的显存余量（字节）；配置坏了回退默认值。"""
        try:
            mb = int(self.settings.get("vram_headroom_mb", _HEADROOM_DEFAULT_MB))
        except (TypeError, ValueError):
            # 配置坏了不该中断通宵批量，退回默认余量继续跑
            log.warning("vram_headroom_mb 配置非法，回退 %d MB", _HEADROOM_DEFAULT_MB)
            mb = _HEADROOM_DEFAULT_MB
        mb = max(_HEADROOM_MIN_MB, min(_HEADROOM_MAX_MB, mb))
        return mb * _MB

    # ------------------------------------------------------------------- vad

    def detect(self, audio: np.ndarray) -> list[tuple[int, int]]:
        """Return speech segments in ms relative to `audio` (int16, 16 kHz)."""
        if self._vad is None:
            raise EngineNotReady("VAD 未加载")
        with self._lock:
            result, _ = self._vad.detect(audio, do_postprocess=True)
        out = []
        for start_s, end_s in result.get("timestamps") or []:
            s = max(0, int(round(start_s * 1000)))
            e = max(s + 120, int(round(end_s * 1000)))
            out.append((s, e))
        return out

    # ------------------------------------------------------------------- asr

    def transcribe(self, items: Sequence[AsrItem], batch_size: int = 6,
                   progress: ProgressFn | None = None,
                   cancelled: CancelFn | None = None,
                   on_batch: Callable[[list[AsrResult]], None] | None = None,
                   ) -> list[AsrResult]:
        """按批识别，OOM 时自愈。

        GPU 显存会随素材变长缓慢抬升，整条任务撞一次 OOM 就白跑。这里有两层
        保护：批前按显存余量主动缩批；真撞 OOM 就把批对半拆开重试。宁可慢，
        也要把整条素材识别完。缩批结果记在 effective_batch_size，本任务只降不升。
        """
        if self._asr is None:
            raise EngineNotReady("识别模型未加载")
        progress = progress or (lambda f, m: None)
        total_s = sum(max(1, it.end_ms - it.start_ms) for it in items) / 1000.0
        done_s = 0.0
        results: list[AsrResult] = []

        # 跨批保守起步：上次缩过批就沿用（不自动回升），但**更小的请求必须生效**——
        # 否则 OOM 降级时 pipeline 传 batch_size=1 会被粘住的旧值吃掉
        # （回归：effective=6 时传 1，仍按 6 编批，降级形同虚设）。
        requested = max(1, int(batch_size))
        if self.effective_batch_size is None or requested < self.effective_batch_size:
            self.effective_batch_size = requested
        pending: list[list[AsrItem]] = list(_bucket(items, self.effective_batch_size))
        headroom = self._headroom_bytes()
        last_delta: int | None = None   # 上一成功批的峰值增量；None = 首批准入
        splits = 0
        recovering = 0                  # >0 表示刚拆过批，等子批成功好记日志
        while pending:
            # 取消检查在每次取批之前，对半拆出来的子批同样经过这里
            if cancelled and cancelled():
                raise Cancelled()
            batch = pending.pop(0)
            cap = self.effective_batch_size or len(batch)
            if len(batch) > cap:
                # 前面已经缩过批：先按记住的大小切片，别整批再撞一次 OOM
                pending[0:0] = [batch[i:i + cap] for i in range(0, len(batch), cap)]
                continue
            state = self._vram_state()
            if len(batch) > 1 and state is not None:
                free, _peak = state
                need = last_delta if last_delta is not None else len(batch) * _SEGMENT_VRAM_BYTES
                # 头部余量是「下限」而不是「加数」：进入显存平台期后 max_memory_allocated 不再增长，
                # need 会退化成 0；若用加法（need*1.3 + headroom），长视频在平台期会被判成「每批都紧张」，
                # 一路级联缩到 batch=1（实测 78 分钟素材平台期空闲约 830MB、默认头部 700MB）。
                # 现在只有「真放不下」（free < 需求×1.3）或「余量跌破下限」（free < headroom）才缩批。
                trigger = max(int(need * 1.3), headroom)
                if free < trigger:
                    left, right = _halve(batch)
                    pending.insert(0, right)
                    pending.insert(0, left)
                    self.effective_batch_size = len(left)
                    splits += 1
                    log.info("按显存余量缩批：空闲 %d MB < 需要 %d MB（需求 %d MB / 头部下限 %d MB），"
                             "batch %d 拆成 %d + %d",
                             free // _MB, trigger // _MB, need // _MB, headroom // _MB,
                             len(batch), len(left), len(right))
                    continue
            before = state[1] if state is not None else None
            try:
                part = self._decode(batch)
            except RuntimeError as e:
                if not _is_oom(e):
                    raise
                self._empty_cache()
                if len(batch) <= 1:
                    log.error("CUDA OOM：单段识别仍然失败，显存不足以继续本任务")
                    raise AsrOutOfMemory(batch_size=1) from e
                left, right = _halve(batch)
                pending.insert(0, right)
                pending.insert(0, left)
                self.effective_batch_size = len(left)
                splits += 1
                recovering = len(left)
                log.warning("CUDA OOM：batch %d 拆成 %d + %d 重试",
                            len(batch), len(left), len(right))
                continue
            after = self._vram_state()
            if before is not None and after is not None:
                # 峰值增量≈这一批的边际显存需求；进入平台期后为 0，判定退化成只查头部余量
                last_delta = max(0, after[1] - before)
            if recovering and len(batch) <= recovering:
                log.info("OOM 后缩批恢复：%d 段批识别成功", len(batch))
                recovering = 0
            results.extend(part)
            for it in batch:
                done_s += max(1, it.end_ms - it.start_ms) / 1000.0
            progress(min(done_s / max(total_s, 0.001), 1.0), f"识别 {len(results)}/{len(items)} 段")
            if on_batch is not None:
                # 契约：回调异常只记日志，绝不能影响识别结果
                try:
                    on_batch(part)
                except Exception:  # noqa: BLE001 - 回调失败属于上游问题，不中断识别
                    log.warning("on_batch 回调异常（已忽略）：batch=%d", len(part), exc_info=True)
        if splits:
            log.info("自适应批处理结束：拆分 %d 次，最终批大小 %d（共 %d 段）",
                     splits, self.effective_batch_size, len(results))
        return results

    def _decode(self, batch: Sequence[AsrItem]) -> list[AsrResult]:
        """跑一批识别并整理成 AsrResult（顺序与 batch 一致，不管进度/回调）。"""
        keys = [f"b{i}" for i in range(len(batch))]
        audios = [(16000, it.audio) for it in batch]
        with self._lock:
            out = self._asr.transcribe(keys, audios)
        by_key = {r.get("uttid"): r for r in out}
        missing = [key for key in keys if key not in by_key]
        if missing:
            # FireRedAsr2.transcribe() swallows its own exceptions
            # (third_party/.../fireredasr2/asr.py:103-105/131-133) and then
            # returns [] or a short list.  Fabricating an empty result for the
            # missing uttids used to mark those segments "done" in the sidecar
            # and delete minutes of speech from the subtitle without any
            # error.  Surface the contract violation instead: the job fails and
            # the already-completed batches stay resumable.
            raise RuntimeError(
                "语音识别未返回 {}/{} 段结果（模型内部异常），本次识别中止"
                .format(len(missing), len(batch)))
        results: list[AsrResult] = []
        for i, it in enumerate(batch):
            r = by_key.get(keys[i]) or {}
            tokens = _tokens_from_timestamp(r.get("timestamp") or [], it.start_ms)
            text = (r.get("text") or "").strip()
            results.append(AsrResult(
                key=it.key,
                text=text,
                tokens=tokens,
                confidence=float(r.get("confidence") or 0.0),
                dur_s=float(r.get("dur_s") or 0.0),
                start_ms=int(it.start_ms),
                end_ms=int(it.end_ms),
            ))
        return results

    # ------------------------------------------------------------------ punc

    def punctuate(self, tokens: Sequence[Token], progress: ProgressFn | None = None,
                  cancelled: CancelFn | None = None) -> list[dict]:
        """Punctuate a flat token stream and return merged sentences.

        Chunks are ~300 characters so BERT's 512-token limit is never hit;
        eight chunks per call keeps the GPU busy without a huge padding bill.
        """
        if self._punc is None:
            raise EngineNotReady("标点模型未加载")
        progress = progress or (lambda f, m: None)
        chunks = list(_split_tokens(tokens, 300))
        batches = [chunks[i:i + 8] for i in range(0, len(chunks), 8)]
        out: list[dict] = []
        for bi, batch in enumerate(batches):
            if cancelled and cancelled():
                raise Cancelled()
            batch_ts = [[t.as_tuple() for t in chunk] for chunk in batch]
            try:
                with self._lock:
                    res = self._punc.process_with_timestamp(
                        batch_ts, [f"p{bi}_{j}" for j in range(len(batch))]
                    )
            except Exception:  # noqa: BLE001
                log.exception("punctuation failed on batch %d/%d", bi + 1, len(batches))
                for chunk in batch:
                    out.extend(_raw_sentences(chunk))
                continue
            for chunk, r in zip(batch, res):
                out.extend(_sentences_from_punc(chunk, r.get("punc_sentences") or []))
            progress((bi + 1) / max(len(batches), 1), f"智能断句 {bi + 1}/{len(batches)}")
        return _merge_sentences(out)


# --------------------------------------------------------------------- helpers


def _tokens_from_timestamp(timestamp: Iterable, offset_ms: int) -> list[Token]:
    tokens: list[Token] = []
    for row in timestamp:
        try:
            text, start_s, end_s = row[0], float(row[1]), float(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        text = (text or "").replace("<blank>", "").replace("<sil>", "")
        if not text:
            continue
        s = offset_ms + int(round(start_s * 1000))
        e = offset_ms + int(round(end_s * 1000))
        if e <= s:
            e = s + 40
        tokens.append(Token(text=text, start_ms=s, end_ms=e))
    return tokens


def _is_oom(err: BaseException) -> bool:
    """各版 torch / 驱动的 OOM 措辞不同，统一按 "out of memory" 判定。"""
    return "out of memory" in str(err).lower()


def _halve(batch: Sequence[AsrItem]) -> tuple[list[AsrItem], list[AsrItem]]:
    """把一批连续切成前后两半（前半略大），拼接后段序与输入一致。"""
    mid = (len(batch) + 1) // 2
    return list(batch[:mid]), list(batch[mid:])


def _bucket(items: Sequence[AsrItem], batch_size: int) -> list[list[AsrItem]]:
    """Group segments of similar length so padded batches stay efficient."""
    ordered = sorted(items, key=lambda it: (it.end_ms - it.start_ms))
    batches: list[list[AsrItem]] = []
    cur: list[AsrItem] = []
    cur_min = 0
    for it in ordered:
        d = max(1, it.end_ms - it.start_ms)
        if cur and (len(cur) >= batch_size or d > cur_min * 2.4):
            batches.append(cur)
            cur = []
        if not cur:
            cur_min = d
        cur.append(it)
    if cur:
        batches.append(cur)
    for b in batches:
        b.sort(key=lambda it: it.start_ms)
    return batches


def _split_tokens(tokens: Sequence[Token], max_chars: int):
    buf: list[Token] = []
    n = 0
    for t in tokens:
        if buf and n + len(t.text) > max_chars:
            yield buf
            buf = []
            n = 0
        buf.append(t)
        n += len(t.text)
    if buf:
        yield buf


def _raw_sentences(tokens: Sequence[Token]) -> list[dict]:
    if not tokens:
        return []
    return [{
        "text": "".join(t.text for t in tokens),
        "start_ms": tokens[0].start_ms,
        "end_ms": tokens[-1].end_ms,
        "confidence": round(sum(t.conf for t in tokens) / len(tokens), 3),
        "tokens": list(tokens),
        "words": [{"text": t.text, "start_ms": t.start_ms, "end_ms": t.end_ms} for t in tokens],
    }]


def _sentences_from_punc(tokens: Sequence[Token], punc_sentences: list[dict]) -> list[dict]:
    if not punc_sentences:
        return _raw_sentences(tokens)
    out: list[dict] = []
    idx = 0
    for ps in punc_sentences:
        text = ps.get("punc_text") or ""
        n_chars = len(content_only(text))
        if n_chars <= 0:
            continue
        take = []
        got = 0
        while idx < len(tokens) and got < n_chars:
            take.append(tokens[idx])
            got += len(tokens[idx].text)
            idx += 1
        if not take:
            continue
        out.append({
            "text": _tidy(text),
            "start_ms": take[0].start_ms,
            "end_ms": take[-1].end_ms,
            "confidence": round(sum(t.conf for t in take) / len(take), 3),
            "tokens": take,
            "words": [{"text": t.text, "start_ms": t.start_ms, "end_ms": t.end_ms} for t in take],
        })
    if idx < len(tokens) and out:
        # punctuation model dropped a tail: append it to the last sentence
        tail = list(tokens[idx:])
        out[-1]["text"] = out[-1]["text"] + "".join(t.text for t in tail)
        out[-1]["end_ms"] = tail[-1].end_ms
        out[-1]["tokens"].extend(tail)
        out[-1]["words"].extend({"text": t.text, "start_ms": t.start_ms, "end_ms": t.end_ms} for t in tail)
    elif idx < len(tokens):
        out.extend(_raw_sentences(list(tokens[idx:])))
    return out


def _tidy(text: str) -> str:
    text = text.strip()
    text = text.replace("  ", " ")
    # no space between CJK and punctuation
    out = []
    for i, ch in enumerate(text):
        if ch == " " and i > 0 and i + 1 < len(text):
            a, b = text[i - 1], text[i + 1]
            if _is_cjk(a) or _is_cjk(b) or a in PUNCT or b in PUNCT:
                continue
        out.append(ch)
    return "".join(out)


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _merge_sentences(sentences: list[dict]) -> list[dict]:
    """Join comma-clauses and dangling ends into readable sentences."""
    merged: list[dict] = []
    for s in sentences:
        if not merged:
            merged.append(dict(s))
            continue
        prev = merged[-1]
        prev_text = (prev.get("text") or "").rstrip()
        gap = s["start_ms"] - prev["end_ms"]
        strong = bool(prev_text) and prev_text[-1] in TERMINAL
        # Keep sentences short enough to review one by one: cap the *result*
        # of a merge, not just the previous clause, so comma chains cannot
        # grow into 100-character blobs.
        next_text = (s.get("text") or "").strip()
        too_long = len(prev_text) + len(next_text) > 64
        if strong or too_long or gap > 1200 or len(prev_text) >= 64:
            merged.append(dict(s))
            continue
        prev["text"] = prev_text + (s.get("text") or "")
        prev["end_ms"] = s["end_ms"]
        prev["tokens"].extend(s.get("tokens") or [])
        prev["words"].extend(s.get("words") or [])
        n = len(prev.get("tokens") or [])
        if n:
            prev["confidence"] = round(
                sum(t.conf for t in prev["tokens"]) / n, 3
            )
    return merged


def probe_gpu() -> dict:
    """Small helper for /system; safe when torch is missing."""
    info = {"cuda": False, "device": "cpu", "name": None, "vram_mb": None}
    try:
        import torch

        info["cuda"] = bool(torch.cuda.is_available())
        if info["cuda"]:
            info["device"] = "cuda"
            info["name"] = torch.cuda.get_device_name(0)
            free, total = torch.cuda.mem_get_info()
            info["vram_mb"] = int(total / 1024 / 1024)
            info["free_mb"] = int(free / 1024 / 1024)
    except Exception:  # noqa: BLE001
        info["device"] = "unavailable"
    return info
