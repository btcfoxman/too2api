"""Caller messages follow the repository-wide normalized message standard."""
from __future__ import annotations


def public_error(code: str, message: str) -> dict[str, str]:
    kind, raw = (code or "UPSTREAM_FAILED").upper(), (message or "").lower()
    if kind in {"UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "UPSTREAM_TIMEOUT"}:
        text = "提交结果未确认，请核对上游任务，暂勿重复提交~"
    elif any(word in raw for word in ("nsfw", "moderation", "community guidelines", "inappropriate", "third-party content")):
        text = "检测到内容有敏感或违规情况，请修改后重试，积分已返还～"
    elif any(word in raw for word in ("image width", "image height", "image dimensions", "image size")):
        text = "参考图片尺寸不符合模型要求，请调整图片尺寸后重试~"
    elif any(word in raw for word in ("duration", "too long", "too short")):
        text = "素材时长不支持，请修改后再试~"
    elif any(word in raw for word in ("too many", "exceeds", "limit exceeded")):
        text = "素材超限，请修改后再试~"
    elif any(word in raw for word in ("unsupported format", "wrong content type", "mime")):
        text = "素材格式不支持，请修改后再试~"
    elif "upload" in raw:
        text = "上游维护中，请稍后再试~"
    elif "download" in raw or kind == "MEDIAERROR":
        text = "素材下载失败，请检查素材链接后重试~"
    elif kind == "ACCOUNT_POOL_UNAVAILABLE" or "not enough credit" in raw or "insufficient credit" in raw:
        text = "上游维护中，请稍后再试~"
    elif kind in {"BROWSERERROR", "UPSTREAM_TEMPORARY"}:
        text = "上游维护中，请稍后再试~"
    else:
        text = "生成失败，积分已返还，请重试~"
    return {"code": kind, "message": text}
