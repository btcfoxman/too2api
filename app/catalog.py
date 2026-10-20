"""Public model aliases and caller media references for Toonkit Quickstart."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    name: str
    upstream: str
    resolution: str
    family: str
    min_seconds: int = 4
    max_seconds: int = 15
    output_resolution: str = ""

    @property
    def limits(self) -> dict[str, int]:
        return {
            "2.5": {"images": 30, "videos": 10, "audios": 10},
            "wan": {"images": 10, "videos": 5, "audios": 5},
            "h3": {"images": 9, "videos": 0, "audios": 0},
        }.get(self.family, {"images": 9, "videos": 3, "audios": 3})


MODELS = {item.name: item for item in (
    ModelSpec("sd-2-0", "SEEDANCE_2_0", "720p", "2.0"),
    ModelSpec("sd-2-0-1080p", "SEEDANCE_2_0", "1080p", "2.0"),
    ModelSpec("sd-2-0-4k", "SEEDANCE_2_0", "4k", "2.0"),
    ModelSpec("sd-2-0-fast", "SEEDANCE_2_0_FAST", "720p", "2.0"),
    ModelSpec("sd-2-0-mini", "SEEDANCE_2_0_MINI", "720p", "2.0"),
    ModelSpec("sd-2-0-mini-480p", "SEEDANCE_2_0_MINI", "720p", "2.0", output_resolution="480p"),
    ModelSpec("sd-2-0-fast-480p", "SEEDANCE_2_0_FAST", "720p", "2.0", output_resolution="480p"),
    ModelSpec("sd-2-5", "SEEDANCE_2_5", "720p", "2.5", max_seconds=30),
    ModelSpec("sd-2-5-480p", "SEEDANCE_2_5", "480p", "2.5", max_seconds=30),
    ModelSpec("sd-2-5-1080p", "SEEDANCE_2_5", "1080p", "2.5", max_seconds=30),
    ModelSpec("wan-3.0", "WAN_3_0", "720p", "wan", max_seconds=30),
    ModelSpec("wan-3.0-480p", "WAN_3_0", "480p", "wan", max_seconds=30),
    ModelSpec("wan-3.0-1080p", "WAN_3_0", "1080p", "wan", max_seconds=30),
    ModelSpec("minimax-h3-768p", "MINIMAX_H3_MAX", "720p", "h3", 5, 15, "768p"),
)}
MUSIC_MODELS = {"suno-v6": "SUNO_V6", "suno-v6-mini": "SUNO_V6_MINI", "suno-v6-wild": "SUNO_V6_WILD"}
RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
REF_RE = re.compile(r"(?<![\w])@?(?P<kind>视频|video|v|视|图片|image|img|图|音频|audio|音)\s*(?P<index>[1-9]\d*)(?!\w)", re.I)


def _media(body: dict[str, Any], plural: str, singular: str) -> list[str]:
    value = body.get(plural, body.get(singular, []))
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{plural} must be an array")
    result = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("url") or item.get("asset_id")
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{plural} contains an invalid item")
        result.append(item.strip())
    return result


def _kind(word: str) -> str:
    value = word.lower()
    if value in {"视频", "video", "v", "视"}:
        return "videos"
    if value in {"图片", "image", "img", "图"}:
        return "images"
    return "audios"


def bind_prompt(prompt: str, media: dict[str, list[Any]]) -> str:
    """Use the localized media labels accepted by the Toonkit composer."""
    labels = {"images": "图片", "videos": "视频", "audios": "音频"}
    def replace(match: re.Match[str]) -> str:
        kind, number = _kind(match.group("kind")), int(match.group("index"))
        if number > len(media[kind]):
            if not media[kind] and not match.group(0).startswith("@"):
                return match.group(0)
            raise ValueError(f"prompt references {match.group(0)} but only {len(media[kind])} {kind} were provided")
        return f"@{labels[kind]}{number}"
    return REF_RE.sub(replace, prompt)


def normalize_request(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("model") or "sd-2-0-mini").strip().lower()
    media = {key: _media(body, key[:-1] + "_urls", key) for key in ("images", "videos", "audios")}
    content = body.get("content")
    prompt_from_content = []
    if content is not None:
        if not isinstance(content, list):
            raise ValueError("content must be an array")
        for item in content:
            if not isinstance(item, dict):
                raise ValueError("content item must be an object")
            item_type = str(item.get("type") or "")
            if item_type in {"text", "input_text"}:
                prompt_from_content.append(str(item.get("text") or ""))
                continue
            kind = {"image_url": "images", "input_image": "images",
                    "video_url": "videos", "audio_url": "audios"}.get(item_type)
            if kind:
                value = item.get(item_type) or item.get("url")
                if isinstance(value, dict):
                    value = value.get("url")
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"invalid {item_type} content URL")
                media[kind].append(value.strip())
    supplied_prompt = str(body.get("prompt") or "").strip()
    if not supplied_prompt:
        supplied_prompt = "\n".join(prompt_from_content).strip()
    if name in MUSIC_MODELS or name == "lyria-3.5":
        if name == "lyria-3.5":
            raise ValueError("lyria-3.5 is not available in this Toonkit account's music capabilities")
        custom = bool(body.get("custom_mode", False))
        prompt = supplied_prompt
        title = str(body.get("title") or "").strip()
        lyrics = str(body.get("lyrics") or "").strip()
        tags = str(body.get("tags") or "").strip()
        if custom and (not title or not lyrics):
            raise ValueError("custom music requires title and lyrics")
        if not custom and not (prompt or tags or lyrics or any(media.values())):
            raise ValueError("music prompt, tags, lyrics or reference media is required")
        if len(prompt) > 3000 or len(tags) > 1000 or len(lyrics) > 5000 or len(title) > 80:
            raise ValueError("music text exceeds Toonkit limits")
        if len(media["images"]) > 5 or len(media["videos"]) > 1 or sum(map(len, media.values())) > 10:
            raise ValueError("music reference media exceeds Toonkit limits")
        return {"model": name, "upstream_model": MUSIC_MODELS[name], "kind": "music",
                "duration": 0, "resolution": "", "aspect_ratio": "", "prompt": prompt,
                "title": title, "lyrics": lyrics, "tags": tags, "custom_mode": custom,
                "make_instrumental": bool(body.get("make_instrumental", False)), **media}
    spec = MODELS.get(name)
    if spec is None:
        raise ValueError(f"unsupported model: {name}")
    prompt = supplied_prompt
    if not prompt:
        raise ValueError("prompt is required")
    raw = body.get("duration", body.get("seconds", 4))
    if isinstance(raw, bool) or not str(raw).isdigit():
        raise ValueError("duration must be an integer number of seconds")
    duration = int(raw)
    if not spec.min_seconds <= duration <= spec.max_seconds:
        raise ValueError(f"duration must be {spec.min_seconds}-{spec.max_seconds} seconds for {name}")
    ratio = str(body.get("aspect_ratio") or body.get("ratio") or "16:9")
    if ratio not in RATIOS or (spec.family == "wan" and ratio == "21:9"):
        raise ValueError(f"unsupported aspect ratio: {ratio}")
    resolution = str(body.get("resolution") or spec.output_resolution or spec.resolution).lower()
    if resolution != (spec.output_resolution or spec.resolution).lower():
        raise ValueError(f"{name} has fixed output resolution {spec.output_resolution or spec.resolution}")
    for kind, limit in spec.limits.items():
        if len(media[kind]) > limit:
            raise ValueError(f"{name} accepts at most {limit} {kind}")
    bind_prompt(prompt, media)
    return {"model": name, "upstream_model": spec.upstream, "kind": "video",
            "duration": duration, "resolution": spec.resolution,
            "output_resolution": spec.output_resolution, "aspect_ratio": ratio,
            "prompt": prompt, "sound": bool(body.get("sound", True)), **media}


def public_models() -> list[dict[str, Any]]:
    videos = [{"id": spec.name, "object": "model", "owned_by": "tooapi",
               "capabilities": {"media_limits": spec.limits, "resolutions": [spec.output_resolution or spec.resolution],
                                "durations": [spec.min_seconds, spec.max_seconds]}} for spec in MODELS.values()]
    music = [{"id": name, "object": "model", "owned_by": "tooapi", "capabilities": {"type": "music"}}
             for name in MUSIC_MODELS]
    return videos + music
