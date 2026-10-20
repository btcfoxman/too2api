"""Local result conversion for public aliases absent from native Toonkit resolution choices."""
from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import tempfile
from pathlib import Path

from .media import _download

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


def result_path(task_id: str) -> Path:
    if not task_id.startswith("too_") or not all(c in "0123456789abcdef" for c in task_id[4:]):
        raise ResultError("invalid task ID")
    return RESULTS / f"{task_id}.mp4"


def convert(url: str, normalized: dict, task_id: str, proxy_url: str) -> str:
    output_res = normalized.get("output_resolution")
    if not output_res:
        return url
    dimensions = DIMENSIONS_480 if output_res == "480p" else DIMENSIONS_768
    target = dimensions.get(normalized["aspect_ratio"])
    if not target:
        raise ResultError("unsupported output aspect ratio")
    data, _, _ = _download(url, "videos", proxy_url)
    RESULTS.mkdir(parents=True, exist_ok=True)
    destination = result_path(task_id)
    with tempfile.TemporaryDirectory(dir=DATA) as folder:
        source = Path(folder) / "upstream.mp4"
        staged = Path(folder) / "converted.mp4"
        source.write_bytes(data)
        width, height = target
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
    base = os.getenv("TOO_PUBLIC_BASE_URL", "https://too2api.aiid.edu.kg").rstrip("/")
    return f"{base}/v1/assets/{task_id}?sig={signature(task_id)}"
