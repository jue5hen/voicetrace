"""Download FireRedASR2S model weights (AED / VAD / Punc) from Hugging Face.

Usage:
    python scripts/download_models.py [--repos aed,vad,punc] [--proxy http://127.0.0.1:7890]

Proxy is opt-in: only --proxy or an existing HTTP(S)_PROXY env var is used;
the script never forces a localhost proxy on its own.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

REPOS = {
    "aed": "FireRedTeam/FireRedASR2-AED",
    "vad": "FireRedTeam/FireRedVAD",
    "lid": "FireRedTeam/FireRedLID",
    "punc": "FireRedTeam/FireRedPunc",
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default="aed,vad,punc")
    ap.add_argument("--proxy", default=None,
                    help="显式代理（如 http://127.0.0.1:7890）；不给则沿用已有 HTTP(S)_PROXY 环境变量，无代理就直连")
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    # 代理 opt-in：只有显式 --proxy 才覆盖环境变量；否则沿用已有 HTTP(S)_PROXY，
    # 不再无条件把流量指向 127.0.0.1:7890（没开代理的机器会直接连接失败）。
    if args.proxy:
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["HTTP_PROXY"] = args.proxy

    from huggingface_hub import snapshot_download

    MODELS.mkdir(parents=True, exist_ok=True)
    for key in args.repos.split(","):
        key = key.strip()
        if not key:
            continue
        repo = REPOS.get(key)
        if repo is None:
            ap.error(f"未知模型 key：{key}（可选：{','.join(REPOS)}）")
        dest = MODELS / key
        t0 = time.time()
        print(f"[{key}] {repo} -> {dest}", flush=True)
        snapshot_download(
            repo_id=repo,
            local_dir=str(dest),
            max_workers=4,
        )
        print(f"[{key}] done in {time.time() - t0:.1f}s, {human(dir_size(dest))}", flush=True)
    print("ALL DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
