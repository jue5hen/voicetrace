from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "VoiceTrace"
APP_NAME_ZH = "声迹"

log = logging.getLogger("voicetrace.config")


def _read_version() -> str:
    """版本唯一真源：#source/version.txt（由 build-release.ps1 注入/断言）。"""
    for cand in (
        Path(__file__).resolve().parents[2] / "version.txt",      # #source/version.txt
        Path(__file__).resolve().parent.parent / "version.txt",  # engine/version.txt（备用）
    ):
        try:
            text = cand.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text.splitlines()[0].strip()
    # 兜底值必须一眼看出是「没读到真源」，不能是一个看起来正常的版本号：
    # 1.3.1 的便携包就因为漏打包 version.txt 而在 /health 里静默报了旧版本。
    log.warning("未找到 version.txt，版本号回落为 0.0.0-dev")
    return "0.0.0-dev"


VERSION = _read_version()

# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS: dict[str, Any] = {
    "device": "auto",              # auto | cuda | cpu
    "half": True,                  # bf16 on Ampere+, fp16 otherwise
    "beam_size": 3,
    "batch_size": 6,
    "gpu_memory_fraction": 0.88,   # CUDA 单进程显存上限（占整卡比例）
    "vram_headroom_mb": 700,       # 每批识别前要留出的显存余量（MB）
    "window_minutes": 30,
    "min_ngram_count": 3,
    "fuzzy_pinyin": False,
    "subtitle_max_chars": 22,
    "subtitle_max_lines": 2,
    "subtitle_clean_fillers": True,
    "subtitle_clean_stutter": True,
    "subtitle_merge_gap_ms": 900,
    "subtitle_punct_space": True,
    "keep_wav": False,
    # 转录完成后自动把 SRT 写到视频所在目录（见 server.Worker._auto_export_srt）。
    "auto_export": True,
    "export_dir": "",
    "reduce_motion": False,
}

_SETTING_LIMITS = {
    "beam_size": (1, 8),
    "batch_size": (1, 16),
    "gpu_memory_fraction": (0.5, 0.95),
    "vram_headroom_mb": (200, 4000),
    "window_minutes": (5, 120),
    "min_ngram_count": (2, 20),
    "subtitle_max_chars": (10, 48),
    "subtitle_max_lines": (1, 3),
    "subtitle_merge_gap_ms": (200, 2000),
}


def _coerce(key: str, value: Any, default: Any) -> Any:
    try:
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(default, int):
            value = int(value)
            lo, hi = _SETTING_LIMITS.get(key, (None, None))
            if lo is not None:
                value = max(lo, min(hi, value))
            return value
        if isinstance(default, float):
            value = float(value)
            lo, hi = _SETTING_LIMITS.get(key, (None, None))
            if lo is not None:
                value = max(lo, min(hi, value))
            return value
        if isinstance(default, str):
            # A JSON null is not a string: str(None) == "None" used to become a
            # literal "None" export_dir, i.e. a junk relative directory.
            if value is None:
                return default
            return str(value)
        return value
    except (TypeError, ValueError):
        return default


def load_settings(path: Path) -> dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in data:
                        data[k] = _coerce(k, v, data[k])
    except Exception:  # noqa: BLE001 - 损坏的设置文件绝不能让启动失败
        log.warning("settings.json 解析失败，回退默认值：%s", path)
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
    if data.get("device") not in ("auto", "cuda", "cpu"):
        data["device"] = "auto"
    return data


_SETTINGS_LOCK = threading.Lock()


def save_settings(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Persist settings atomically; the caller only publishes on success.

    The old implementation wrote a fixed settings.tmp with no lock: two
    concurrent PUTs could clobber the staging file, and a failed write left
    the in-memory settings already cleared by the caller.
    """
    clean = {k: _coerce(k, settings.get(k, v), v) for k, v in DEFAULT_SETTINGS.items()}
    clean["device"] = clean["device"] if clean["device"] in ("auto", "cuda", "cpu") else "auto"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SETTINGS_LOCK:
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(clean, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
    return clean


# ---------------------------------------------------------------- paths


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ("." + APP_NAME.lower())


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


@dataclass
class Paths:
    data_dir: Path
    models_dir: Path
    ffmpeg_dir: Path | None = None
    engine_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "voicetrace.db"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def transcript_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure(self) -> None:
        for d in (self.data_dir, self.transcript_dir, self.thumb_dir,
                  self.cache_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --- tool discovery -------------------------------------------------
    def ffmpeg(self) -> Path:
        return self._find_tool("ffmpeg")

    def ffprobe(self) -> Path:
        return self._find_tool("ffprobe")

    def _find_tool(self, name: str) -> Path:
        exe = name + (".exe" if os.name == "nt" else "")
        candidates: list[Path] = []
        if self.ffmpeg_dir:
            candidates.append(Path(self.ffmpeg_dir) / exe)
        env_dir = os.environ.get("VT_FFMPEG") or ""
        if env_dir:
            candidates.append(Path(env_dir) / exe)
        candidates.append(self.engine_root / "bin" / exe)
        for c in candidates:
            if c.is_file():
                return c
        found = shutil.which(name)
        if found:
            return Path(found)
        raise FileNotFoundError(f"{exe} not found; pass --ffmpeg <dir>")

    def model(self, key: str) -> Path:
        p = self.models_dir / key
        if not p.is_dir():
            raise FileNotFoundError(f"model '{key}' missing at {p}")
        return p

    def third_party(self) -> Path:
        """FireRedASR2S source root (fireredasr2s package lives here)."""
        candidates = [
            self.engine_root / "third_party" / "FireRedASR2S",
            self.engine_root / "vendor" / "FireRedASR2S",
        ]
        for c in candidates:
            if (c / "fireredasr2s").is_dir():
                return c
        # fall back to site-packages style import
        return candidates[0]


def resolve_paths(args: Any) -> Paths:
    """Resolve CLI/config paths.

    ``--engine-root`` used to be parsed and then ignored: models/, third_party/
    and bin/ always resolved against the package parent, so pointing the flag
    at another tree silently did nothing.  The flag now decides the root (and
    the default models directory under it); omitting it keeps the old default.
    """
    root_arg = str(getattr(args, "engine_root", "") or "").strip()
    engine_root = (Path(root_arg).expanduser().resolve() if root_arg
                   else Path(__file__).resolve().parent.parent)
    data_dir = Path(getattr(args, "data_dir", "") or default_data_dir()).expanduser().resolve()
    models_dir = Path(getattr(args, "models_dir", "") or (engine_root / "models")).expanduser().resolve()
    ffdir = getattr(args, "ffmpeg", "") or ""
    p = Paths(data_dir=data_dir, models_dir=models_dir,
              ffmpeg_dir=Path(ffdir).resolve() if ffdir else None,
              engine_root=engine_root)
    p.ensure()
    return p


def python_executable() -> str:
    return sys.executable
