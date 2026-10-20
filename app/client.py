"""Toonkit authenticated web protocol from the captured Quickstart requests."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from .browser import BrowserRejected, CDPBrowser, account_proxy_url
from .catalog import bind_prompt
from .media import _download, upload_media_bytes
from .store import Store

_LOCK_GUARD = threading.RLock()
_MEDIA_LOCKS: dict[int, threading.RLock] = {}


def account_media_lock(account_id: int) -> threading.RLock:
    with _LOCK_GUARD:
        return _MEDIA_LOCKS.setdefault(account_id, threading.RLock())


class ToonkitError(Exception):
    pass


def _ids(assets: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    return {kind: [str(item["toonkit_asset_id"]) for item in assets[kind]]
            for kind in ("images", "videos", "audios")}


def _authorized_media_url(url: str, token: str) -> str:
    parts = urlsplit(url)
    if parts.hostname not in {"private-cdn.toonkit.io", "rx6afwxj12499.edge.naverncp.com"} or not token:
        return url
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("token", token)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def build_video_body(normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ids = _ids(assets)
    mode = "REFERENCE_TO_VIDEO" if (any(ids.values()) or normalized["resolution"] in {"1080p", "4k"}
                                    or normalized["upstream_model"] == "MINIMAX_H3_MAX") else "TEXT_TO_VIDEO"
    prompt = bind_prompt(normalized["prompt"], ids)
    return {"modelId": normalized["upstream_model"], "mode": mode, "prompt": prompt,
            "resolution": normalized["resolution"], "aspectRatio": normalized["aspect_ratio"],
            "durationSeconds": normalized["duration"], "shareToCommunity": False,
            "sound": normalized.get("sound", True), "referenceImageIds": ids["images"],
            "referenceVideoIds": ids["videos"], "referenceAudioIds": ids["audios"],
            "referenceVideoTotalSeconds": round(sum(float(item.get("duration") or 0)
                                                    for item in assets["videos"]), 3),
            "count": 1, "clientRequestId": uuid.uuid4().hex[:26]}


def build_music_body(normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ids = _ids(assets)
    result: dict[str, Any] = {"modelId": normalized["upstream_model"],
        "mode": "CUSTOM" if normalized["custom_mode"] else "DEFAULT",
        "instrumental": normalized["make_instrumental"], "count": 1,
        "style": normalized["tags"], "lyrics": normalized["lyrics"],
        "referenceImageIds": ids["images"], "referenceVideoIds": ids["videos"],
        "referenceAudioIds": ids["audios"], "clientRequestId": uuid.uuid4().hex[:26]}
    if normalized["custom_mode"]:
        result["title"] = normalized["title"]
    else:
        result["prompt"] = normalized["prompt"]
    return result


class ToonkitClient:
    def __init__(self, account: dict[str, Any], *, store: Store | None = None):
        account_proxy_url(account)
        self.account = account
        self.store = store
        self.browser = CDPBrowser(int(account["cdp_port"]))

    def account_state(self) -> dict[str, Any]:
        data = self.browser.get("/api/v1/users/me")
        if not data.get("id"):
            raise BrowserRejected("Toonkit account login is incomplete")
        credit = data.get("credit") or {}
        return {"available_balance": float(credit.get("totalBalance") or 0),
                "buckets": credit}

    def ensure_no_watermark(self) -> str:
        return "not_applicable"

    def daily_checkin(self) -> dict[str, Any]:
        # The captured /signup-credit/claim is a one-time signup grant, not a
        # verified daily claim. Avoid repeatedly invoking it as a daily action.
        return {"already_checked_in": True, "receive_quota": 0}

    def upload_inputs(self, normalized: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {"images": [], "videos": [], "audios": []}
        account_id = int(self.account["id"])
        proxy = str(self.account.get("proxy_url") or "")
        for kind in result:
            for source in normalized[kind]:
                if source.startswith("toonkit:"):
                    result[kind].append({"toonkit_asset_id": source[8:]})
                    continue
                data, mime, filename = _download(source, kind, proxy)
                digest = hashlib.sha256(data).hexdigest()
                cached = self.store.media_cache_get(account_id, kind, digest) if self.store else None
                if cached:
                    result[kind].append(cached)
                    continue
                uploaded = upload_media_bytes(data, mime, filename, kind,
                                              browser=self.browser, proxy_url=proxy)
                if self.store:
                    self.store.media_cache_put(account_id, kind, digest, uploaded)
                result[kind].append(uploaded)
        return result

    def submit(self, normalized: dict[str, Any], assets: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
        if normalized["kind"] == "music":
            body = build_music_body(normalized, assets)
            path = "/api/v1/quickstart/music"
            quote = {"kind": "MUSIC", "music": {"surface": "PLAYGROUND", "settings":
                {key: value for key, value in body.items() if key != "clientRequestId"}}}
        else:
            body = build_video_body(normalized, assets)
            path = "/api/v1/quickstart/batch/videos"
            quote = {"kind": "VIDEO", "video": {"surface": "PLAYGROUND",
                "modelId": body["modelId"], "mode": body["mode"],
                "resolution": body["resolution"], "durationSeconds": body["durationSeconds"],
                "aspectRatio": body["aspectRatio"], "generateAudio": body["sound"],
                "referenceImageIds": body["referenceImageIds"],
                "referenceVideoIds": body["referenceVideoIds"],
                "referenceAudioIds": body["referenceAudioIds"]}}
        quoted = self.browser.post("/api/v1/generation/credit-quote", quote)
        cost = float(quoted.get("creditCost") or quoted.get("totalCost") or 0)
        balance = self.account_state()["available_balance"]
        if cost and balance < cost:
            raise BrowserRejected(f"not enough credits: quote {cost}, balance {balance}")
        try:
            response = self.browser.post(path, body)
        except BrowserRejected as exc:
            # A definitive 4xx asset validation means generation was not
            # accepted. Reupload once; keep caller task ID and avoid retrying
            # ambiguous transport failures that might duplicate a paid task.
            message = str(exc).lower()
            if any(word in message for word in ("reference", "asset", "upload", "media validation")):
                if self.store:
                    self.store.clear_media_cache(int(self.account["id"]))
                fresh = self.upload_inputs(normalized)
                body = build_music_body(normalized, fresh) if normalized["kind"] == "music" else build_video_body(normalized, fresh)
                response = self.browser.post(path, body)
            else:
                raise
        items = response.get("itemIds") or []
        if not isinstance(items, list) or not items:
            raise ToonkitError("Toonkit accepted a generation without pollable media IDs")
        return {"run_id": str(response.get("generationId") or response.get("id") or items[0]),
                "thread_id": json.dumps(items, separators=(",", ":"))}

    def poll(self, run_id: str, thread_id: str) -> dict[str, Any]:
        try:
            items = json.loads(thread_id)
        except ValueError as exc:
            raise ToonkitError("stored Toonkit media IDs are invalid") from exc
        results = [self.browser.get(f"/api/v1/quickstart/media/{item}/status") for item in items]
        errors = [item for item in results if str(item.get("status") or "").upper() in {"FAILED", "ERROR", "REJECTED"}]
        if errors:
            reason = errors[0].get("errorMessage") or errors[0].get("errorCode") or "generation failed"
            return {"status": "failed", "result_urls": [], "error": str(reason),
                    "error_code": str(errors[0].get("errorCode") or "")}
        if all(str(item.get("status") or "").upper() == "COMPLETED" for item in results):
            urls = [str(item.get("outputAudioUrl") or item.get("outputVideoUrl") or "") for item in results]
            if not all(urls):
                # The per-item status endpoint can report COMPLETED while its
                # output URL stays null; the media listing includes the URL.
                listing = self.browser.get("/api/v1/quickstart/media?limit=100")
                by_id = {str(item.get("id")): item for item in listing.get("items") or []}
                urls = [str((by_id.get(str(item_id)) or {}).get("outputAudioUrl") or
                            (by_id.get(str(item_id)) or {}).get("outputVideoUrl") or "")
                        for item_id in items]
            if all(urls):
                token = self.browser.media_token()
                return {"status": "succeeded", "result_urls":
                        [_authorized_media_url(url, token) for url in urls], "error": ""}
        return {"status": "running", "result_urls": [], "error": ""}
