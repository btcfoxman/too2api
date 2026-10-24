from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import socket
import threading
from unittest.mock import Mock

import pytest

from app import result_transfer as transfer


STORAGE = {"result_s3_center_api_base": "http://storage.test", "result_s3_center_token": "fixture-token",
           "result_s3_bucket": "assets", "result_s3_key_prefix": "aicontent/ai-too"}
TASK = "too_0123456789abcdef"
NORMALIZED = {"model": "wan-3.0", "kind": "video", "upstream_model": "WAN_3_0", "duration": 4,
              "resolution": "720p", "aspect_ratio": "16:9", "images": [], "videos": [], "audios": []}


def future():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()




@pytest.fixture
def public_origin():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(403 if self.path == "/forbidden" else 206)
            self.send_header("Content-Type", "text/html" if self.path == "/html" else "audio/mpeg" if self.path == "/music" else "video/mp4")
            self.end_headers()
            self.wfile.write(b"<html>blocked</html>" if self.path == "/html" else
                             (b"ID3" if self.path == "/music" else b"\x00\x00\x00\x18ftypisom") + b"0" * 1024)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_port), seen
    server.shutdown()
    server.server_close()


def test_public_probe_is_direct_unauthenticated_and_rejects_html_403(public_origin, monkeypatch):
    base, seen = public_origin
    # The loopback origin tests HTTP behavior; public-address policy is tested
    # separately below and must reject loopback in normal operation.
    monkeypatch.setattr(transfer, "_public_url", lambda url: url.startswith(base + "/"))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    assert transfer.public_reachable(base + "/video", "video")
    assert not transfer.public_reachable(base + "/html", "video")
    assert not transfer.public_reachable(base + "/forbidden", "video")
    assert all(headers["Range"] == "bytes=0-8191" and "Authorization" not in headers and "Cookie" not in headers for headers in seen)


def test_music_probe_requires_audio_bytes_and_uses_no_auth_or_proxy(public_origin, monkeypatch):
    base, seen = public_origin
    monkeypatch.setattr(transfer, "_public_url", lambda url: url.startswith(base + "/"))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    assert transfer.public_reachable(base + "/music", "music")
    assert not transfer.public_reachable(base + "/video", "music")
    assert not transfer.public_reachable(base + "/html", "music")
    assert all("Cookie" not in headers and "Authorization" not in headers for headers in seen)


def test_storage_defaults_use_too_environment_and_prefix(monkeypatch):
    monkeypatch.setenv("TOO_S3_CENTER_API_BASE", "http://storage.test/")
    monkeypatch.setenv("TOO_S3_CENTER_TOKEN", "fixture-token")
    monkeypatch.delenv("TOO_S3_CENTER_BUCKET", raising=False)
    monkeypatch.delenv("TOO_S3_CENTER_KEY_PREFIX", raising=False)
    monkeypatch.setenv("JDC_S3_CENTER_API_BASE", "http://unrelated.test")
    assert transfer.config() == ("http://storage.test", "fixture-token", "assets", "aicontent/ai-too")


@pytest.mark.parametrize("expiry", ["", "invalid", "2020-01-01T00:00:00Z", "2020-01-01T00:00:00"])
def test_missing_invalid_or_old_presign_requires_refresh(expiry):
    assert transfer.expires_soon({"expires_at": expiry})
    assert not transfer.expires_soon({"expires_at": future()})


@pytest.mark.parametrize("url", ["http://127.0.0.1/video", "https://10.0.0.1/video", "http://192.168.3.5/video",
    "http://[::1]/video", "https://169.254.169.254/video", "https://224.0.0.1/video", "https://user@8.8.8.8/video",
    "https://8.8.8.8:8443/video", "https://8.8.8.8:/video", "https://8.8.8.8/video#part", "https://8.8.8.8/video#",
    "https://8.8.8.8:bad/video", "https://[bad/video", "file:///video", "https://8.8.8.8/vi\ndeo"])
def test_public_address_policy_rejects_private_or_nonportable_urls(url):
    assert not transfer._public_url(url)


def test_public_address_policy_checks_all_dns_answers(monkeypatch):
    def addresses(*values):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443)) for value in values]
    monkeypatch.setattr(transfer.socket, "getaddrinfo", lambda *args, **kwargs: addresses("8.8.8.8", "1.1.1.1"))
    assert transfer._public_url("https://media.test/video")
    assert transfer._public_url("https://[2001:4860:4860::8888]/video")
    monkeypatch.setattr(transfer.socket, "getaddrinfo", lambda *args, **kwargs: addresses("8.8.8.8", "10.0.0.1"))
    assert not transfer._public_url("https://media.test/video")
    monkeypatch.setattr(transfer.socket, "getaddrinfo", Mock(side_effect=socket.gaierror()))
    assert not transfer._public_url("https://media.test/video")


def test_public_probe_checks_redirect_before_request_and_does_not_forward_cookies(monkeypatch):
    requests = []
    monkeypatch.setattr(transfer.socket, "getaddrinfo", lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    class HTTP:
        cookies = Mock()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            requests.append((url, kwargs))
            result = Response(headers={"Location": "http://192.168.3.5/private", "Set-Cookie": "secret=fixture"})
            result.status_code = 302
            return result
    monkeypatch.setattr(transfer.requests, "Session", HTTP)
    assert not transfer.public_reachable("https://media.test/video", "video")
    assert len(requests) == 1 and requests[0][1]["allow_redirects"] is False
    assert "Cookie" not in requests[0][1]["headers"] and "Authorization" not in requests[0][1]["headers"]
    assert HTTP.cookies.clear.call_count == 1


def test_public_original_is_retained_without_local_download(monkeypatch):
    monkeypatch.setattr(transfer, "public_reachable", lambda *args: True)
    monkeypatch.setattr(transfer, "persist", lambda *args: pytest.fail("public media downloaded"))
    url, meta = transfer.deliver("https://public.test/video.mp4", NORMALIZED, TASK, "", 0)
    assert url == "https://public.test/video.mp4" and meta["backend"] == "upstream"


def test_legacy_asset_uses_local_cache_and_never_counts_as_public(tmp_path, monkeypatch):
    cached = tmp_path / "cached.mp4"
    cached.write_bytes(b"x" * 2048)
    monkeypatch.setattr(transfer, "result_path", lambda *args: cached)
    monkeypatch.setattr(transfer, "public_reachable", lambda *args: pytest.fail("legacy asset publicly probed"))
    monkeypatch.setattr(transfer, "persist", lambda *args: pytest.fail("legacy cached result downloaded"))
    monkeypatch.setattr(transfer, "upload_cached", lambda *args: ("https://s3.test/object", {"backend": "s3_center_direct"}))
    url, meta = transfer.deliver("https://too.test/v1/assets/" + TASK, NORMALIZED, TASK, "", 0)
    assert url == "https://s3.test/object" and "/v1/assets/" in meta["legacy_url"]


def test_required_conversion_still_caches_then_uploads(tmp_path, monkeypatch):
    cached = tmp_path / "converted.mp4"
    monkeypatch.setattr(transfer, "result_path", lambda *args: cached)
    monkeypatch.setattr(transfer, "public_reachable", lambda *args: pytest.fail("conversion bypassed"))
    calls = []
    def persist(*args):
        calls.append(args[1]["output_resolution"])
        cached.write_bytes(b"x" * 2048)
    monkeypatch.setattr(transfer, "persist", persist)
    monkeypatch.setattr(transfer, "upload_cached", lambda *args: ("https://s3.test/converted", {}))
    assert transfer.deliver("https://upstream.test/video", {**NORMALIZED, "output_resolution": "480p"}, TASK, "", 0)[0] == "https://s3.test/converted"
    assert calls == ["480p"]
















class Response:
    def __init__(self, value=None, headers=None):
        self.status_code, self.value, self.headers = 200, value, headers or {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def json(self):
        return self.value


def storage_http(monkeypatch, *, corrupt=False, already_committed=False, fail_abort=False,
                 bad_session=None, fail_presign=0, conflict=False, missing_response=None):
    state = {"puts": [], "posts": [], "parts": [], "idempotency": [], "deletes": [], "gets": [],
             "sessions": {}, "fail_presign": fail_presign, "create_keys": [], "create_bodies": []}
    class HTTP:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, *, json, headers, **kwargs):
            state["posts"].append(url)
            assert headers["Authorization"] == "Bearer fixture-token" and kwargs["allow_redirects"] is False
            if url.endswith(("/v1/uploads", "/v1/multipart")):
                state["body"] = json
                state["create_bodies"].append(dict(json))
                state["idempotency"].append(headers["Idempotency-Key"])
                session = state["sessions"].get(headers["Idempotency-Key"])
                if session is None:
                    session = {"id": "du_" + format(len(state["sessions"]) + 1, "032x"),
                               "status": "committed" if already_committed else "url_issued",
                               "declared_bytes": json["size"], "logical_key": json["bucket"] + "/" + json["key"],
                               "expires_at": future()}
                    state["sessions"][headers["Idempotency-Key"]] = session
                state["session"] = session
                if session["status"] != "url_issued" or conflict and ":retry:" not in headers["Idempotency-Key"]:
                    response = Response({"error": {"code": "session_conflict"}})
                    response.status_code = 409
                    return response
                if url.endswith("/v1/multipart"):
                    created = dict(session)
                else:
                    # Actual s3-center single-create JSON intentionally omits
                    # logical_key. GET session and complete provide it.
                    created = {key: value for key, value in session.items() if key != "logical_key"}
                    created["upload"] = {"method": "PUT", "url": "https://objects.test/upload",
                        "headers": {"Content-Type": [json["content_type"]], "X-Amz-Meta-Upload-Id": [session["id"]]}, "expires_at": future()}
                state["create_keys"].append(set(created))
                return Response(created)
            if url.endswith("/parts/presign"):
                state["parts"].append(json["part_number"])
                return Response({"part_number": json["part_number"], "request": {"method": "PUT", "url": "https://objects.test/part", "headers": {}, "expires_at": future()}})
            if url.endswith("/complete"):
                state["session"]["status"] = "committed"
                return Response({**state["session"], "declared_bytes": state["body"]["size"] + int(corrupt)})
            assert url.endswith("/v1/objects/presign")
            if missing_response is not None:
                response = Response({"error": {"code": missing_response[1]}})
                response.status_code = missing_response[0]
                return response
            if state["fail_presign"]:
                state["fail_presign"] -= 1
                response = Response({"error": {"code": "access_settings_unavailable"}})
                response.status_code = 503
                return response
            if not any(session["status"] == "committed" for session in state["sessions"].values()):
                response = Response({"error": {"code": "object_not_found"}})
                response.status_code = 404
                return response
            return Response({"bucket": json["bucket"], "key": json["key"], "size_bytes": state["body"]["size"], "request": {"method": "GET", "url": "https://objects.test/download", "headers": {}, "expires_at": future()}})
        def get(self, url, *, headers, **kwargs):
            assert url.endswith("/v1/uploads/" + state["session"]["id"])
            assert headers == {"Authorization": "Bearer fixture-token"} and kwargs["allow_redirects"] is False
            state["gets"].append(url)
            return Response({**state["session"], **(bad_session or {})})
        def put(self, url, *, headers, data, **kwargs):
            assert "Authorization" not in headers and kwargs["allow_redirects"] is False
            state["puts"].append(data.read())
            return Response(headers={"ETag": '"part-etag"'})
        def delete(self, url, *, headers, **kwargs):
            assert url.endswith("/abort") and kwargs["allow_redirects"] is False
            assert headers == {"Authorization": "Bearer fixture-token"}
            state["deletes"].append(url)
            if fail_abort:
                raise transfer.requests.ConnectionError("abort fixture failure")
            state["session"]["status"] = "aborted"
            return Response({"status": "aborted"})
    monkeypatch.setattr(transfer.requests, "Session", HTTP)
    return state


def test_upload_validates_metadata_normalizes_headers_and_uses_idempotency(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 2048)
    state = storage_http(monkeypatch)
    url, metadata = transfer.upload_cached(path, TASK, 0, "video", "https://upstream.test/video", STORAGE)
    assert url == "https://objects.test/download" and metadata["upstream_url"] == "https://upstream.test/video"
    assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() and state["puts"] == [path.read_bytes()]
    assert state["idempotency"][0].startswith("too2api:" + TASK) and metadata["upload_mode"] == "single"
    assert state["create_keys"] == [{"id", "status", "declared_bytes", "expires_at", "upload"}]
    assert len(state["gets"]) == 1
    storage_http(monkeypatch, corrupt=True)
    with pytest.raises(transfer.ResultTransferError, match="metadata"):
        transfer.upload_cached(path, TASK, 0, "video", "https://upstream.test/video", STORAGE)


def test_committed_idempotent_upload_only_refreshes_url(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 2048)
    state = storage_http(monkeypatch, already_committed=True)
    assert transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)[0].startswith("https://objects.test/")
    assert state["puts"] == [] and state["gets"] == []
    assert len(state["idempotency"]) == 1


def test_two_music_outputs_have_distinct_keys_sessions_and_audio_content_type(tmp_path, monkeypatch):
    state = storage_http(monkeypatch)
    results = []
    for index, content in enumerate((b"a" * 2048, b"b" * 2048)):
        path = tmp_path / f"music-{index}.mp3"
        path.write_bytes(content)
        results.append(transfer.upload_cached(path, TASK, index, "music", f"https://original.test/{index}.mp3", STORAGE)[1])
    assert len({item["key"] for item in results}) == 2 and len(state["sessions"]) == 2
    assert [item["index"] for item in results] == [0, 1]
    assert all(item["key"].startswith("aicontent/ai-too/" + TASK + "/") and item["key"].endswith(".mp3") for item in results)
    assert all(item["content_type"] == "audio/mpeg" for item in state["create_bodies"])
    assert len(set(state["idempotency"])) == 2 and state["puts"] == [b"a" * 2048, b"b" * 2048]


def test_legacy_music_results_use_each_indexed_cache_without_redownload(tmp_path, monkeypatch):
    cached = {}
    for index in range(2):
        cached[index] = tmp_path / f"old-{index}.mp3"
        cached[index].write_bytes(b"x" * 2048)
    monkeypatch.setattr(transfer, "result_path", lambda task_id, index, kind: cached[index])
    monkeypatch.setattr(transfer, "persist", lambda *args: pytest.fail("legacy music downloaded again"))
    monkeypatch.setattr(transfer, "public_reachable", lambda *args: pytest.fail("legacy asset retained as public"))
    uploaded = Mock(side_effect=lambda path, task_id, index, *args:
                    (f"https://objects.test/{index}.mp3", {"backend": "s3_center_direct", "index": index}))
    monkeypatch.setattr(transfer, "upload_cached", uploaded)
    for index in range(2):
        url, metadata = transfer.deliver(f"https://too.test/v1/assets/{TASK}/{index}", {"kind": "music"}, TASK, "", index, STORAGE)
        assert url == f"https://objects.test/{index}.mp3" and metadata["index"] == index and metadata["legacy_url"]
    assert [call.args[0] for call in uploaded.call_args_list] == [cached[0], cached[1]]
    assert all(call.args[4] == "" for call in uploaded.call_args_list)


@pytest.mark.parametrize("size", [1023, 500 * 1024 * 1024 + 1])
def test_cache_size_limit_is_checked_before_open_or_upload(size):
    path = Mock()
    path.stat.return_value.st_size = size
    with pytest.raises(transfer.ResultTransferError, match="500 MiB"):
        transfer.upload_cached(path, TASK, 0, "music", "https://original.test/audio", STORAGE)
    path.open.assert_not_called()


@pytest.mark.parametrize("wrong", [{"id": "du_" + "f" * 32}, {"logical_key": "assets/other/object"},
    {"declared_bytes": 2049}, {"declared_bytes": "2048"}, {"declared_bytes": True}, {"status": "aborted"}])
def test_get_session_must_match_exact_object_before_upload(tmp_path, monkeypatch, wrong):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 2048)
    state = storage_http(monkeypatch, bad_session=wrong)
    with pytest.raises(transfer.ResultTransferError):
        transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    assert state["puts"] == []


def test_real_commit_then_presign_failure_recovers_from_409_without_reupload(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 2048)
    monkeypatch.setattr(transfer.time, "sleep", lambda seconds: None)
    state = storage_http(monkeypatch, fail_presign=3)
    with pytest.raises(transfer.ControlHTTPError, match="503"):
        transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    url, metadata = transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    assert url == "https://objects.test/download" and metadata["recovered_committed"] is True
    assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(state["puts"]) == 1 and sum(url.endswith("/complete") for url in state["posts"]) == 1
    assert state["idempotency"][0] == state["idempotency"][1]


def test_aborted_multipart_retries_in_new_storage_session_only_after_exact_missing_object(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 2048)
    monkeypatch.setattr(transfer, "SINGLE_UPLOAD_BYTES", 1024)
    state = storage_http(monkeypatch)
    original_put = transfer._put
    monkeypatch.setattr(transfer, "_put", Mock(side_effect=transfer.ResultTransferError("part interrupted")))
    with pytest.raises(transfer.ResultTransferError, match="part interrupted"):
        transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    monkeypatch.setattr(transfer, "_put", original_put)
    _, metadata = transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    assert state["idempotency"][0] == state["idempotency"][1]
    assert state["idempotency"][2].startswith(state["idempotency"][0] + ":retry:")
    assert len(state["sessions"]) == 2 and metadata["upload_id"] == state["session"]["id"]
    assert len(state["deletes"]) == 1 and len(state["puts"]) == 1


@pytest.mark.parametrize("missing", [(404, "not_found"), (404, ""), (403, "forbidden"), (503, "unavailable")])
def test_conflict_never_starts_new_session_for_unknown_404_forbidden_or_storage_failure(tmp_path, monkeypatch, missing):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 2048)
    monkeypatch.setattr(transfer.time, "sleep", lambda seconds: None)
    state = storage_http(monkeypatch, conflict=True, missing_response=missing)
    with pytest.raises(transfer.ControlHTTPError):
        transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    assert len(state["idempotency"]) == 1 and not state["puts"] and not state["gets"]


def test_multipart_uses_bounded_parts_and_same_commit_validation(tmp_path, monkeypatch):
    assert transfer.SINGLE_UPLOAD_BYTES == 256 * 1024 * 1024
    assert transfer.PART_BYTES == 16 * 1024 * 1024
    assert len(range(0, transfer.MAX_MEDIA_BYTES, transfer.PART_BYTES)) == 32
    path = tmp_path / "large.mp4"
    path.write_bytes(b"x" * 2500)
    monkeypatch.setattr(transfer, "SINGLE_UPLOAD_BYTES", 1024)
    monkeypatch.setattr(transfer, "PART_BYTES", 1024)
    state = storage_http(monkeypatch)
    _, metadata = transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    assert state["parts"] == [1, 2, 3] and list(map(len, state["puts"])) == [1024, 1024, 452]
    assert metadata["upload_mode"] == "multipart" and b"".join(state["puts"]) == path.read_bytes()
    assert state["create_keys"] == [{"id", "status", "logical_key", "declared_bytes", "expires_at"}]


@pytest.mark.parametrize("fail_abort", [False, True])
def test_multipart_failure_aborts_best_effort_and_preserves_original_error(tmp_path, monkeypatch, fail_abort):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 2048)
    monkeypatch.setattr(transfer, "SINGLE_UPLOAD_BYTES", 1024)
    state = storage_http(monkeypatch, fail_abort=fail_abort)
    monkeypatch.setattr(transfer, "_put", Mock(side_effect=transfer.ResultTransferError("original part failure")))
    with pytest.raises(transfer.ResultTransferError, match="original part failure"):
        transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    assert len(state["deletes"]) == 1
    assert not any(url.endswith("/complete") for url in state["posts"])


def test_signed_get_failure_does_not_abort_committed_multipart(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * 2048)
    monkeypatch.setattr(transfer, "SINGLE_UPLOAD_BYTES", 1024)
    state = storage_http(monkeypatch)
    monkeypatch.setattr(transfer, "presign", Mock(side_effect=transfer.ResultTransferError("signed GET unavailable")))
    with pytest.raises(transfer.ResultTransferError, match="signed GET unavailable"):
        transfer.upload_cached(path, TASK, 0, "video", "https://original.test", STORAGE)
    assert any(url.endswith("/complete") for url in state["posts"])
    assert state["deletes"] == []


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer forbidden"}, {"x-amz-meta-x": "bad\r\nvalue"}, {"x": [1]}])
def test_invalid_presigned_headers_are_rejected(headers):
    with pytest.raises(transfer.ResultTransferError):
        transfer._headers(headers)


def test_presign_size_mismatch_and_required_get_headers_are_rejected(monkeypatch):
    storage_http(monkeypatch)
    monkeypatch.setattr(transfer, "_post", lambda *args, **kwargs: {"bucket": "assets", "key": "aicontent/ai-too/x", "size_bytes": 99,
        "request": {"method": "GET", "url": "https://objects.test/x", "headers": {}, "expires_at": future()}})
    with pytest.raises(transfer.ResultTransferError, match="does not match"):
        transfer.presign({"bucket": "assets", "key": "aicontent/ai-too/x", "size_bytes": 2048}, STORAGE)
