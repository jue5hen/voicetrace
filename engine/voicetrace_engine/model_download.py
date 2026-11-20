"""Download the open-source FireRedASR2S model weights used by VoiceTrace.

The desktop UI starts this module through :mod:`voicetrace_engine.downloader`
in a child process, so a download can be cancelled without touching the engine.

Usage::

    python -m voicetrace_engine.model_download --models-dir <dir>
        [--repos vad,punc,aed]
        [--proxy http://127.0.0.1:7890]
        [--endpoint https://hf-mirror.com]

Exit code 0 = every requested model is complete, 3 = at least one failed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPOS = {
    "aed": "FireRedTeam/FireRedASR2-AED",
    "vad": "FireRedTeam/FireRedVAD",
    "punc": "FireRedTeam/FireRedPunc",
}

LABELS = {
    "aed": "语音识别模型",
    "vad": "语音活动检测模型",
    "punc": "标点模型",
}

# Keep in sync with `scripts/build-release.ps1` (Get-ModelRequirements):
# only the files the inference code actually loads, with a minimum size so a
# half-finished download can never be treated as ready.
REQUIRED: dict[str, tuple[tuple[str, int], ...]] = {
    "aed": (
        ("model.pth.tar", 4 << 30),
        ("cmvn.ark", 1 << 10),
        ("dict.txt", 10 << 10),
        ("train_bpe1000.model", 100 << 10),
    ),
    "vad": (
        ("VAD/cmvn.ark", 1 << 10),
        ("VAD/model.pth.tar", 1 << 20),
    ),
    "punc": (
        ("model.pth.tar", 300 << 20),
        ("chinese-bert-wwm-ext_vocab.txt", 50 << 10),
        ("out_dict", 16),
        ("chinese-lert-base/config.json", 100),
        ("chinese-lert-base/pytorch_model.bin", 300 << 20),
        ("chinese-lert-base/vocab.txt", 50 << 10),
        ("chinese-lert-base/tokenizer.json", 100 << 10),
    ),
}

# Approximate payload per model (bytes), used for progress reporting only.
EXPECTED_BYTES = {
    "aed": 4_735_000_000,
    "vad": 7_100_000,
    "punc": 816_000_000,
}

DEFAULT_ORDER = ("vad", "punc", "aed")


def _file_ok(path: Path, min_bytes: int) -> bool:
    try:
        return path.stat().st_size >= min_bytes
    except OSError:
        return False


def is_model_ready(models_dir: Path | str, key: str) -> bool:
    """True only when every required file exists and is big enough."""
    base = Path(models_dir) / key
    return all(_file_ok(base / rel, size) for rel, size in REQUIRED[key])


def missing_models(models_dir: Path | str, keys: tuple[str, ...] = tuple(REPOS)) -> list[str]:
    return [key for key in keys if not is_model_ready(models_dir, key)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download VoiceTrace model weights")
    ap.add_argument("--models-dir", required=True)
    ap.add_argument("--repos", default=",".join(DEFAULT_ORDER))
    ap.add_argument("--proxy", default="", help="HTTP(S) proxy, e.g. http://127.0.0.1:7890")
    ap.add_argument("--endpoint", default="", help="HuggingFace endpoint, e.g. https://hf-mirror.com")
    args = ap.parse_args(argv)

    if args.proxy:
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["HTTP_PROXY"] = args.proxy
        os.environ["ALL_PROXY"] = args.proxy
    if args.endpoint:
        os.environ["HF_ENDPOINT"] = args.endpoint
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    models = Path(args.models_dir)
    models.mkdir(parents=True, exist_ok=True)

    from huggingface_hub import snapshot_download

    keys = [k.strip() for k in args.repos.split(",") if k.strip()]
    if not keys:
        print("no models requested", flush=True)
        return 3

    failed = False
    for key in keys:
        if key not in REPOS:
            print(f"[{key}] unknown model key", flush=True)
            failed = True
            break
        if is_model_ready(models, key):
            print(f"[{key}] already complete", flush=True)
            continue
        print(f"[{key}] downloading {REPOS[key]} -> {models / key}", flush=True)
        try:
            snapshot_download(
                repo_id=REPOS[key],
                local_dir=str(models / key),
                max_workers=4,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI via exit code
            print(f"[{key}] failed: {type(exc).__name__}: {exc}", flush=True)
            failed = True
            break
        if not is_model_ready(models, key):
            print(f"[{key}] incomplete after download", flush=True)
            failed = True
            break
        print(f"[{key}] complete", flush=True)

    print("FAILED" if failed else "DONE", flush=True)
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
