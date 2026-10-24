"""Deliver publicly usable originals, otherwise upload validated cached media to s3-center."""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests

from .media import MAX_MEDIA_BYTES
from .result import persist, result_path


SINGLE_UPLOAD_BYTES = 256 * 1024 * 1024
PART_BYTES = 16 * 1024 * 1024
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class ResultTransferError(Exception):
    pass


class ControlHTTPError(ResultTransferError):
    def __init__(self, status_code: int, error_code: str = ""):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"s3-center control HTTP {status_code}")


def config(storage: dict | None = None) -> tuple[str, str, str, str]:
    storage = storage or {}
    base = str(storage.get("result_s3_center_api_base") or os.getenv("TOO_S3_CENTER_API_BASE", "")).strip().rstrip("/")
    token = str(storage.get("result_s3_center_token") or os.getenv("TOO_S3_CENTER_TOKEN", "")).strip()
    bucket = str(storage.get("result_s3_bucket") or os.getenv("TOO_S3_CENTER_BUCKET", "assets")).strip()
    prefix = str(storage.get("result_s3_key_prefix") or os.getenv("TOO_S3_CENTER_KEY_PREFIX", "aicontent/ai-too")).strip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not token or not bucket):
        raise ResultTransferError("s3-center API base, token and bucket must be configured")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,99}", prefix) or "//" in prefix
            or any(part in {".", ".."} for part in prefix.split("/"))):
        raise ResultTransferError("s3-center object key prefix is invalid")
    return base, token, bucket, prefix


def _url(value: object) -> str:
    value = str(value or "")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ResultTransferError("s3-center returned an invalid signed URL")
    return value


def _expiry(value: object) -> str:
    value = str(value or "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.timestamp() <= time.time():
            raise ValueError()
    except ValueError as exc:
        raise ResultTransferError("s3-center returned an invalid expiry") from exc
    return value


def expires_soon(metadata: dict) -> bool:
    try:
        value = datetime.fromisoformat(str(metadata.get("expires_at") or "").replace("Z", "+00:00"))
        return value.tzinfo is None or value.timestamp() <= time.time() + 300
    except ValueError:
        return True


def _headers(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ResultTransferError("s3-center upload headers are invalid")
    result = {}
    for name, raw in value.items():
        if (not isinstance(name, str) or not name or any(char in name for char in "\r\n:")
                or name.lower() in {"authorization", "proxy-authorization", "cookie"}):
            raise ResultTransferError("s3-center upload header name is invalid")
        text = raw if isinstance(raw, str) else ",".join(raw) if isinstance(raw, list) and all(isinstance(v, str) for v in raw) else ""
        if not text or text[:1].isspace() or any(char in text for char in "\r\n"):
            raise ResultTransferError("s3-center upload header value is invalid")
        result[name] = text
    return result


def _control(http, method: str, url: str, token: str, body: dict | None = None, *, idempotency: str = "") -> dict:
    headers = {"Authorization": "Bearer " + token}
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    for attempt in range(3):
        try:
            options = {"headers": headers, "timeout": (15, 60), "allow_redirects": False}
            if method == "post":
                options["json"] = body
            with getattr(http, method)(url, **options) as response:
                if response.status_code in RETRY_STATUSES and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                if not 200 <= response.status_code < 300:
                    code = ""
                    try:
                        value = response.json()
                        error = value.get("error") if isinstance(value, dict) else None
                        if isinstance(error, dict) and error.get("code") in {"session_conflict", "session_expired", "object_not_found"}:
                            code = error["code"]
                    except ValueError:
                        pass
                    raise ControlHTTPError(response.status_code, code)
                try:
                    value = response.json()
                except ValueError as exc:
                    raise ResultTransferError("s3-center returned invalid JSON") from exc
                if not isinstance(value, dict):
                    raise ResultTransferError("s3-center returned invalid JSON")
                return value
        except requests.RequestException as exc:
            if attempt == 2:
                raise ResultTransferError("s3-center transport failure: " + type(exc).__name__) from exc
            time.sleep(2 ** attempt)
    raise ResultTransferError("s3-center request failed")


def _post(http, url: str, token: str, body: dict | None, *, idempotency: str = "") -> dict:
    return _control(http, "post", url, token, body, idempotency=idempotency)


def _session(value: dict, size: int, logical: str, expected_status: str | None = None,
             *, require_logical: bool = True) -> str:
    identity = str(value.get("id") or "")
    if (not re.fullmatch(r"du_[a-f0-9]{32}", identity)
            or ((require_logical or "logical_key" in value) and value.get("logical_key") != logical)
            or type(value.get("declared_bytes")) is not int or value.get("declared_bytes") != size
            or value.get("status") not in ({expected_status} if expected_status else {"url_issued", "committed"})):
        raise ResultTransferError("s3-center upload metadata does not match object")
    return identity


def _put(http, grant: dict, body_factory) -> str:
    if grant.get("method") != "PUT":
        raise ResultTransferError("s3-center upload method must be PUT")
    url, headers = _url(grant.get("url")), _headers(grant.get("headers"))
    _expiry(grant.get("expires_at"))
    for attempt in range(3):
        try:
            with body_factory() as contents:
                with http.put(url, headers=headers, data=contents, timeout=(20, 300), allow_redirects=False) as response:
                    if 200 <= response.status_code < 300:
                        return str(response.headers.get("ETag") or "").strip()
                    if response.status_code not in RETRY_STATUSES or attempt == 2:
                        raise ResultTransferError(f"s3-center upload HTTP {response.status_code}")
        except requests.RequestException as exc:
            if attempt == 2:
                raise ResultTransferError("s3-center upload transport failure: " + type(exc).__name__) from exc
        time.sleep(2 ** attempt)
    raise ResultTransferError("s3-center upload failed")


def presign(metadata: dict, storage: dict | None = None) -> tuple[str, dict]:
    base, token, bucket, prefix = config(storage)
    key, size = str(metadata.get("key") or ""), metadata.get("size_bytes")
    if metadata.get("bucket") != bucket or not key.startswith(prefix + "/") or not isinstance(size, int) or size <= 0:
        raise ResultTransferError("s3-center stored object metadata is invalid")
    with requests.Session() as http:
        http.trust_env = False
        grant = _post(http, base + "/v1/objects/presign", token,
                      {"bucket": bucket, "key": key, "method": "GET", "expires_seconds": 0})
    request = grant.get("request") or {}
    if (grant.get("bucket") != bucket or grant.get("key") != key or grant.get("size_bytes") != size
            or request.get("method") != "GET" or _headers(request.get("headers", {}))):
        raise ResultTransferError("s3-center signed GET does not match object")
    url = _url(request.get("url"))
    return url, {**metadata, "url": url, "expires_at": _expiry(request.get("expires_at"))}


def upload_cached(path: Path, task_id: str, index: int, kind: str, upstream_url: str,
                  storage: dict | None = None) -> tuple[str, dict]:
    base, token, bucket, prefix = config(storage)
    size = path.stat().st_size
    if not 1024 <= size <= MAX_MEDIA_BYTES:
        raise ResultTransferError("cached result must be between 1 KiB and 500 MiB")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(part)
    content_hash = digest.hexdigest()
    extension = "png" if kind == "image" else "mp3" if kind in {"speech", "music"} else "mp4"
    mime = "image/png" if kind == "image" else "audio/mpeg" if kind in {"speech", "music"} else "video/mp4"
    key = f"{prefix}/{task_id}/{index}-{content_hash[:24]}.{extension}"
    logical = f"{bucket}/{key}"
    multipart = size > SINGLE_UPLOAD_BYTES
    root = "/v1/multipart" if multipart else "/v1/uploads"
    idempotency = f"too2api:{task_id}:{index}:{content_hash}"
    metadata = {"backend": "s3_center_direct", "bucket": bucket, "key": key, "size_bytes": size,
                "sha256": content_hash, "upload_id": "", "upload_mode": "multipart" if multipart else "single",
                "upstream_url": upstream_url, "index": index}
    with requests.Session() as http:
        http.trust_env = False
        body = {"bucket": bucket, "key": key, "size": size, "content_type": mime}
        try:
            created = _post(http, base + root, token, body, idempotency=idempotency)
        except ControlHTTPError as conflict:
            if (conflict.status_code, conflict.error_code) not in {(409, "session_conflict"), (410, "session_expired")}:
                raise
            # The real service rejects reuse of committed/aborted/expired
            # sessions. Recover only an exact existing object; a missing object
            # permits a fresh storage session, with HTTP retries sharing its key.
            try:
                return presign({**metadata, "recovered_committed": True}, storage)
            except ControlHTTPError as lookup:
                if lookup.status_code != 404 or lookup.error_code != "object_not_found":
                    raise
            created = _post(http, base + root, token, body,
                            idempotency=idempotency + ":retry:" + uuid.uuid4().hex)
        # Single create omits logical_key by protocol; multipart includes it.
        # Read back this exact session before PUT, then verify it again at commit.
        upload_id = _session(created, size, logical, "url_issued", require_logical=multipart)
        _expiry(created.get("expires_at"))
        saved = _control(http, "get", f"{base}/v1/uploads/{upload_id}", token)
        if _session(saved, size, logical) != upload_id:
            raise ResultTransferError("s3-center saved upload ID changed")
        metadata["upload_id"] = upload_id
        if saved["status"] != "committed":
            try:
                completed = _upload_parts(http, base, root, token, upload_id, created, path, size, multipart)
            except Exception:
                if multipart:
                    try:
                        with http.delete(f"{base}{root}/{quote(upload_id, safe='')}/abort",
                                         headers={"Authorization": "Bearer " + token},
                                         timeout=(15, 30), allow_redirects=False):
                            pass
                    except Exception:
                        pass  # Preserve the original failure; cleanup is best effort.
                raise
            if _session(completed, size, logical, "committed") != upload_id:
                raise ResultTransferError("s3-center completed upload ID changed")
    return presign(metadata, storage)


def _upload_parts(http, base, root, token, upload_id, created, path, size, multipart):
    from contextlib import contextmanager
    import io

    # Completion is the commit boundary. A later signed-GET failure must never
    # abort a committed object; a retry reuses the deterministic idempotency key.
    if multipart:
        parts = []
        for number, offset in enumerate(range(0, size, PART_BYTES), 1):
            grant = _post(http, f"{base}{root}/{upload_id}/parts/presign", token, {"part_number": number})
            if grant.get("part_number") != number:
                raise ResultTransferError("s3-center returned an unexpected part number")
            @contextmanager
            def contents(offset=offset):
                with path.open("rb") as source:
                    source.seek(offset)
                    body = source.read(min(PART_BYTES, size - offset))
                if len(body) != min(PART_BYTES, size - offset):
                    raise ResultTransferError("cached result changed during multipart upload")
                with io.BytesIO(body) as stream:
                    yield stream
            etag = _put(http, grant.get("request") or {}, contents)
            if not etag or "\r" in etag or "\n" in etag:
                raise ResultTransferError("s3-center multipart part ETag is missing")
            parts.append({"part_number": number, "etag": etag})
        complete_body = {"parts": parts}
    else:
        _put(http, created.get("upload") or {}, lambda: path.open("rb"))
        complete_body = None
    return _post(http, f"{base}{root}/{quote(upload_id, safe='')}/complete", token, complete_body)


def _public_url(url: str) -> bool:
    """Only publish standard-port URLs whose every DNS address is public."""
    try:
        if not isinstance(url, str) or any(ord(char) < 33 for char in url) or "#" in url:
            return False
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 80, 443} or parsed.netloc.endswith(":")):
            return False
        hostname = parsed.hostname
        if "%" in hostname:
            return False
        try:
            addresses = [ipaddress.ip_address(hostname)]
        except ValueError:
            addresses = [ipaddress.ip_address(row[4][0]) for row in
                         socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                                            type=socket.SOCK_STREAM)]
        return bool(addresses) and all(address.is_global and not address.is_multicast for address in addresses)
    except (ValueError, OSError):
        return False


def public_reachable(url: str, kind: str) -> bool:
    """Direct unauthenticated ranged GET, checking public DNS before every hop."""
    try:
        with requests.Session() as http:
            http.trust_env = False
            for hop in range(6):
                if not _public_url(url):
                    return False
                http.cookies.clear()
                with http.get(url, headers={"Range": "bytes=0-8191", "Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0"},
                              proxies={}, timeout=(8, 15), stream=True, allow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if hop == 5 or not location:
                            return False
                        url = urljoin(url, location)
                        continue
                    if response.status_code not in {200, 206}:
                        return False
                    content_type = response.headers.get("Content-Type", "").lower()
                    if any(v in content_type for v in ("html", "json", "text/")):
                        return False
                    first = next(response.iter_content(8192), b"")
                    if kind == "image":
                        return first.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")) or first.startswith(b"RIFF") and first[8:12] == b"WEBP"
                    if kind in {"music", "speech"}:
                        return first.startswith((b"ID3", b"RIFF", b"OggS", b"fLaC")) or len(first) > 1 and first[0] == 255 and first[1] & 224 == 224
                    return len(first) > 11 and (first[4:8] == b"ftyp" or first.startswith(b"\x1aE\xdf\xa3"))
    except (requests.RequestException, ValueError, StopIteration):
        return False
    return False


def deliver(url: str, normalized: dict, task_id: str, proxy_url: str, index: int,
            storage: dict | None = None) -> tuple[str, dict]:
    kind = normalized.get("kind", "video")
    converted = bool(normalized.get("output_resolution")) and kind == "video"
    if "/v1/assets/" not in url and not converted and public_reachable(url, kind):
        return url, {"backend": "upstream", "upstream_url": url, "url": url, "index": index, "checked_at": time.time()}
    path = result_path(task_id, index, kind)
    if not path.exists():
        if "/v1/assets/" in url:
            raise ResultTransferError("legacy local result cache is unavailable; refresh original upstream run")
        # Existing validation, download fallback, ffprobe and conversion are reused.
        persist(url, normalized, task_id, proxy_url, index)
    signed, metadata = upload_cached(path, task_id, index, kind, "" if "/v1/assets/" in url else url, storage)
    if "/v1/assets/" in url:
        metadata["legacy_url"] = url
    return signed, metadata
