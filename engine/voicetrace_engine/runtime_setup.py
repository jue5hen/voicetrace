"""Download and install the CUDA PyTorch runtime on demand.

The installer ships without PyTorch because the CUDA build is far larger than
what a single NSIS installer (2 GB limit) can hold.  The desktop UI starts this
module in a child process: it downloads the two official wheels next to the
runtime and then installs them into ``runtime/site-packages`` with pip.

Usage::

    python -m voicetrace_engine.runtime_setup --site-packages <dir>
        --download-dir <dir> [--proxy http://127.0.0.1:7890] [--mirror]

Exit code 0 = torch + torchaudio installed, 3 = failed.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

WHEELS = (
    {
        "filename": "torch-2.1.0+cu118-cp311-cp311-win_amd64.whl",
        "size": 2_722_704_093,
        "urls": (
            "https://download.pytorch.org/whl/cu118/torch-2.1.0%2Bcu118-cp311-cp311-win_amd64.whl",
            "https://mirrors.aliyun.com/pytorch-wheels/cu118/torch-2.1.0%2Bcu118-cp311-cp311-win_amd64.whl",
        ),
    },
    {
        "filename": "torchaudio-2.1.0+cu118-cp311-cp311-win_amd64.whl",
        "size": 3_922_031,
        "urls": (
            "https://download.pytorch.org/whl/cu118/torchaudio-2.1.0%2Bcu118-cp311-cp311-win_amd64.whl",
            "https://mirrors.aliyun.com/pytorch-wheels/cu118/torchaudio-2.1.0%2Bcu118-cp311-cp311-win_amd64.whl",
        ),
    },
)

TOTAL_BYTES = sum(w["size"] for w in WHEELS)


def torch_ready() -> bool:
    """True when both torch and torchaudio can be imported (no heavy import)."""
    try:
        return (
            importlib.util.find_spec("torch") is not None
            and importlib.util.find_spec("torchaudio") is not None
        )
    except (ImportError, ValueError):
        return False


def installed_ok(site_packages: Path) -> bool:
    dll = site_packages / "torch" / "lib" / "torch_cuda.dll"
    audio = site_packages / "torchaudio" / "__init__.py"
    try:
        return dll.stat().st_size > 500 * 1024 * 1024 and audio.is_file()
    except OSError:
        return False


def _download(urls: tuple[str, ...], dest: Path, expected: int, proxy: str) -> None:
    import requests

    part = dest.with_suffix(dest.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    for url in urls:
        have = part.stat().st_size if part.exists() else 0
        if have >= expected:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            with requests.get(
                url,
                headers=headers,
                proxies=proxies,
                stream=True,
                timeout=(20, 120),
                allow_redirects=True,
            ) as res:
                res.raise_for_status()
                mode = "ab" if have and res.status_code == 206 else "wb"
                if mode == "wb":
                    have = 0
                print(f"[download] {url} ({res.status_code})", flush=True)
                with part.open(mode) as fh:
                    for chunk in res.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            print(f"[download] failed {url}: {type(exc).__name__}: {exc}", flush=True)
            continue
        if part.stat().st_size >= expected:
            break
    if not part.exists() or part.stat().st_size < expected:
        got = part.stat().st_size if part.exists() else 0
        raise RuntimeError(f"{dest.name} 下载不完整：{got}/{expected} 字节")
    part.replace(dest)
    print(f"[download] done {dest.name}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install the VoiceTrace CUDA runtime")
    ap.add_argument("--site-packages", required=True)
    ap.add_argument("--download-dir", required=True)
    ap.add_argument("--proxy", default="")
    ap.add_argument("--mirror", action="store_true", help="prefer the Aliyun mirror")
    args = ap.parse_args(argv)

    site_packages = Path(args.site_packages)
    download_dir = Path(args.download_dir)
    site_packages.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    if torch_ready() and installed_ok(site_packages):
        print("ALREADY INSTALLED", flush=True)
        return 0

    wheels: list[Path] = []
    for item in WHEELS:
        dest = download_dir / item["filename"]
        if dest.exists() and dest.stat().st_size == item["size"]:
            print(f"[download] cached {dest.name}", flush=True)
        else:
            urls = item["urls"]
            if args.mirror:
                urls = tuple(reversed(urls))
            print(f"[download] start {dest.name} ({item['size']} bytes)", flush=True)
            _download(urls, dest, item["size"], args.proxy)
        wheels.append(dest)

    print("PHASE install", flush=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-index",
        "--upgrade",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--target",
        str(site_packages),
        *[str(w) for w in wheels],
    ]
    env = os.environ.copy()
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    print("[install] " + " ".join(cmd), flush=True)
    code = subprocess.call(cmd, env=env)
    if code != 0:
        print(f"[install] pip exited with {code}", flush=True)
        return 3
    if not installed_ok(site_packages):
        print("[install] verification failed", flush=True)
        return 3
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
