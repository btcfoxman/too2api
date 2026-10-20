"""Persist Toonkit outputs and convert aliases absent from native resolutions."""
from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from .media import MAX_MEDIA_BYTES, _proxy

DATA = Path(os.getenv("TOO_DATA_DIR", "/app/data"))
RESULTS = DATA / "results"
DIMENSIONS_480 = {"16:9": (864, 496), "4:3": (752, 560), "1:1": (640, 640),
                  "3:4": (560, 752), "9:16": (496, 864), "21:9": (992, 432)}
DIMENSIONS_768 = {"16:9": (1366, 768), "4:3": (1024, 768), "1:1": (768, 768),
                  "3:4": (768, 1024), "9:16": (768, 1366), "21:9": (1792, 768)}


class ResultError(Exception):
    pass


def signature(task_id: str) -> str:
    key = os.getenv("TOO_API_KEY", "").encode()
    return hmac.new(key, task_id.encode(), hashlib.sha256).hexdigest()


def result_path(task_id: str, index: int = 0, kind: str = "video") -> Path:
    if (not task_id.startswith("too_") or not all(c in "0123456789abcdef" for c in task_id[4:])
            or not 0 <= index <= 20 or kind not in {"video", "music"}):
        raise ResultError("invalid task result path")
    return RESULTS / f"{task_id}_{index}.{'mp3' if kind == 'music' else 'mp4'}"


def _download_result(url: str, destination: Path, proxy_url: str) -> None:
    routes = [("direct", {}), ("account proxy", _proxy(proxy_url)),
              ("default proxy", _proxy(os.getenv("TOO_DEFAULT_MEDIA_PROXY", "socks5://host.docker.internal:10808")))]
    failures = []
    for name, proxies in routes:
        if name != "direct" and not proxies:
            continue
        for attempt in range(3):
            session = requests.Session()
            session.trust_env = False
            try:
                # Toonkit's private CDN serves browser video as a ranged GET;
                # HEAD and occasional plain GET calls return 403.
                with session.get(url, headers={"Range": "bytes=0-", "User-Agent": "Mozilla/5.0"},
                                 proxies=proxies, timeout=(10, 90), stream=True) as response:
                    response.raise_for_status()
                    size = 0
                    with destination.open("wb") as output:
                        for chunk in response.iter_content(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_MEDIA_BYTES:
                                raise ResultError("upstream result exceeds 500 MB")
                            output.write(chunk)
                if size < 1024:
                    raise ResultError("upstream result is empty")
                return
            except requests.RequestException as exc:
                failures.append(f"{name}:{type(exc).__name__}")
                if attempt < 2:
                    time.sleep(attempt + 1)
            finally:
                session.close()
    raise ResultError("upstream result download failed after direct and proxy retries: " + ", ".join(failures))


def persist(url: str, normalized: dict, task_id: str, proxy_url: str, index: int = 0) -> str:
    kind = normalized["kind"]
    suffix = ".mp3" if kind == "music" else ".mp4"
    RESULTS.mkdir(parents=True, exist_ok=True)
    destination = result_path(task_id, index, kind)
    if destination.is_file() and destination.stat().st_size >= 1024:
        return _signed_url(task_id, index)
    with tempfile.TemporaryDirectory(dir=DATA) as folder:
        source = Path(folder) / ("upstream" + suffix)
        _download_result(url, source, proxy_url)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                               capture_output=True, text=True, timeout=30)
        if probe.returncode or not probe.stdout.strip() or float(probe.stdout.strip()) <= 0:
            raise ResultError("upstream result is not playable media")
        staged = source
        output_res = normalized.get("output_resolution")
        if output_res and kind == "video":
            dimensions = DIMENSIONS_480 if output_res == "480p" else DIMENSIONS_768
            target = dimensions.get(normalized["aspect_ratio"])
            if not target:
                raise ResultError("unsupported output aspect ratio")
            width, height = target
            staged = Path(folder) / "converted.mp4"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac",
                "-movflags", "+faststart", str(staged)]
            try:
                subprocess.run(command, capture_output=True, timeout=300, check=True)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ResultError("result conversion failed") from exc
            if staged.stat().st_size < 1024:
                raise ResultError("result conversion produced an invalid video")
        staged.replace(destination)
    return _signed_url(task_id, index)


def _signed_url(task_id: str, index: int) -> str:
    base = os.getenv("TOO_PUBLIC_BASE_URL", "https://too2api.aiid.edu.kg").rstrip("/")
    return f"{base}/v1/assets/{task_id}/{index}?sig={signature(task_id)}"
