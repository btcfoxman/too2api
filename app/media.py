"""Download caller media and upload it to Toonkit's presigned object store."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .browser import CDPBrowser

MAX_MEDIA_BYTES = 500 * 1024 * 1024
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
              "video/mp4": ".mp4", "video/quicktime": ".mov", "audio/mpeg": ".mp3",
              "audio/wav": ".wav", "audio/mp4": ".m4a"}


class MediaError(Exception):
    pass


def _proxy(value: str) -> dict[str, str]:
    value = value.replace("socks5://", "socks5h://", 1)
    return {"http": value, "https": value} if value else {}


def _download(source: str, kind: str, proxy_url: str = "") -> tuple[bytes, str, str]:
    if source.startswith("data:"):
        header, sep, payload = source.partition(",")
        if not sep or ";base64" not in header:
            raise MediaError("reference data URL must be base64 encoded")
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise MediaError("reference data URL is invalid") from exc
        mime = header[5:].split(";", 1)[0].lower()
        filename = kind + EXTENSIONS.get(mime, ".bin")
    elif source.startswith(("https://", "http://")):
        parsed = urlsplit(source)
        if not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("reference URL is invalid")
        routes = [("direct", {}), ("account proxy", _proxy(proxy_url)),
                  ("default proxy", _proxy(os.getenv("TOO_DEFAULT_MEDIA_PROXY", "socks5://host.docker.internal:10808")))]
        failures = []
        for label, route in routes:
            if label == "account proxy" and not route:
                continue
            for attempt in range(2):
                session = requests.Session()
                session.trust_env = False
                try:
                    with session.get(source, proxies=route, timeout=(8, 40), stream=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as response:
                        response.raise_for_status()
                        chunks, size = [], 0
                        for chunk in response.iter_content(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_MEDIA_BYTES:
                                raise MediaError("reference media exceeds 500 MB")
                            chunks.append(chunk)
                        data = b"".join(chunks)
                        mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                        filename = Path(parsed.path).name or kind + EXTENSIONS.get(mime, ".bin")
                    break
                except MediaError:
                    raise
                except requests.RequestException as exc:
                    failures.append(f"{label}:{type(exc).__name__}")
                    if attempt == 0:
                        time.sleep(.5)
                finally:
                    session.close()
            else:
                continue
            break
        else:
            raise MediaError("reference media download failed after direct and proxy retries: " + ", ".join(failures))
    else:
        raise MediaError("reference media must be an HTTP URL or base64 data URL")
    if not data:
        raise MediaError("reference media is empty")
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaError("reference media exceeds 500 MB")
    expected = {"images": "image/", "videos": "video/", "audios": "audio/"}[kind]
    if not mime.startswith(expected):
        guessed = mimetypes.guess_type(filename)[0] or ""
        if not guessed.startswith(expected):
            raise MediaError("reference media has wrong content type")
        mime = guessed
    return data, mime, filename


def duration_seconds(data: bytes, suffix: str) -> float:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        path = Path(handle.name)
        handle.write(data)
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                                capture_output=True, text=True, timeout=25, check=True)
        return round(float(result.stdout.strip()), 3)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise MediaError("reference clip duration could not be determined") from exc
    finally:
        path.unlink(missing_ok=True)


def upload_media_bytes(data: bytes, mime: str, filename: str, kind: str, *,
                       browser: CDPBrowser, proxy_url: str) -> dict:
    """A confirmed Toonkit reference gets an account scoped asset ID."""
    session = browser.post("/api/v1/uploads", {"intent": "QUICKSTART_REFERENCE",
        "fileName": filename, "contentType": mime, "size": len(data), "animated": False})
    upload_url = str(session.get("presignedUrl") or "")
    upload_id = str(session.get("sessionId") or "")
    if not upload_id or urlsplit(upload_url).scheme != "https":
        raise MediaError("Toonkit did not provide a valid upload session")
    proxies = _proxy(proxy_url)
    errors = []
    for attempt in range(3):
        try:
            with requests.Session() as http:
                http.trust_env = False
                result = http.put(upload_url, data=data, headers={"Content-Type": mime},
                                  proxies=proxies, timeout=(10, 120))
                result.raise_for_status()
            break
        except requests.RequestException as exc:
            errors.append(type(exc).__name__)
            if attempt == 2:
                raise MediaError("Toonkit reference upload failed: " + ", ".join(errors)) from exc
            time.sleep(attempt + 1)
    body: dict[str, object] = {"sessionId": upload_id}
    if kind in {"videos", "audios"}:
        body["durationSeconds"] = duration_seconds(data, EXTENSIONS.get(mime, ".bin"))
    confirmed = browser.post("/api/v1/uploads/confirm", body)
    asset_id = str(confirmed.get("id") or "")
    if not asset_id:
        raise MediaError("Toonkit did not confirm uploaded reference media")
    return {"toonkit_asset_id": asset_id, "duration": body.get("durationSeconds", 0),
            "bytes": len(data), "mime": mime}
