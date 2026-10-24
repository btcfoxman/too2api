import importlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.catalog import normalize_request
from app.result_transfer import ResultTransferError
from app.service import Service
from app.store import Store


TASK = "too_0123456789abcdef"
SOURCES = ["https://original.test/first.mp3", "https://original.test/second.mp3"]
STORAGE = {"result_s3_center_api_base": "http://storage.test", "result_s3_center_token": "fixture-token",
           "result_s3_bucket": "assets", "result_s3_key_prefix": "aicontent/ai-too"}


def metadata(index, *, expired=False):
    return {"backend": "s3_center_direct", "url": f"https://s3.test/{index}.mp3",
            "bucket": "assets", "key": f"aicontent/ai-too/{TASK}/{index}.mp3", "index": index,
            "expires_at": "2000-01-01T00:00:00Z" if expired else "2099-01-01T00:00:00Z"}


@pytest.fixture
def service(tmp_path, monkeypatch):
    # No browser or real HTTP is needed by these lifecycle tests.
    for key in ("TOO_S3_CENTER_API_BASE", "TOO_S3_CENTER_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    store = Store(str(tmp_path / "test.sqlite3"))
    account = store.upsert_account({"name": "fixture", "proxy_url": "socks5://fixture.test:1080", "cdp_port": 12345})
    store.update_account(account["id"], {"status": "active", "balance": 20})
    request = {"model": "suno-v6", "prompt": "fixture song"}
    store.create_task(TASK, request, normalize_request(request))
    store.update_task(TASK, {"status": "succeeded", "progress": 100, "account_id": account["id"],
        "run_id": "original-run", "thread_id": '["clip-first","clip-second"]', "actual_cost": 15,
        "completed_at": time.time(), "result_urls": SOURCES})
    value = Service(store)
    monkeypatch.setattr("app.service.result_path", lambda *args: tmp_path / "existing.mp3")
    (tmp_path / "existing.mp3").write_bytes(b"ID3" + b"0" * 2048)
    yield value
    value.stop()


def test_old_database_adds_only_empty_transfer_state_and_preserves_rows(service):
    before = service.store.task(TASK)
    with sqlite3.connect(service.store.path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN result_transfer_json")
    migrated = Store(str(service.store.path))
    assert migrated.task(TASK) == before
    assert migrated.task(TASK)["result_transfer"] == {}


def test_unconfigured_legacy_poll_and_retry_queue_do_not_change_history(service, monkeypatch):
    service._started = True
    monkeypatch.setattr(service, "_storage_configured", lambda: False)
    queued = Mock()
    monkeypatch.setattr(service._result_pool, "submit", queued)
    before = service.store.task(TASK)
    public = service.prepare_public_task(before)
    service.retry_pending_results()
    assert public["status"] == "running" and public["progress"] == 95 and public["data"] == []
    assert service.store.task(TASK) == before
    queued.assert_not_called()


def test_delivery_retry_preserves_billing_and_first_clip_checkpoint(service, monkeypatch):
    deliver = Mock(side_effect=[(metadata(0)["url"], metadata(0)), ResultTransferError("storage unavailable")])
    monkeypatch.setattr("app.service.deliver", deliver)
    result = service.persist_completed_task(TASK)
    task = service.store.task(TASK)
    assert result["status"] == "running" and not result["data"]
    assert task["status"] == "succeeded" and task["actual_cost"] == 15
    assert task["run_id"] == "original-run" and json.loads(task["thread_id"]) == ["clip-first", "clip-second"]
    assert task["result_transfer"]["items"] == [metadata(0), {}]
    assert task["result_transfer"]["upstream_urls"] == SOURCES
    assert service.store.pending_result_tasks()[0]["id"] == TASK
    deliver.reset_mock(side_effect=True)
    deliver.return_value = metadata(1)["url"], metadata(1)
    result = service.persist_completed_task(TASK)
    assert result["data"] == [{"url": metadata(0)["url"]}, {"url": metadata(1)["url"]}]
    deliver.assert_called_once()
    assert deliver.call_args.args[4] == 1
    assert service.store.task(TASK)["actual_cost"] == 15


def test_existing_public_originals_remain_original_and_ordered(service, monkeypatch):
    def deliver(source, normalized, task_id, proxy, index, storage):
        return source, {"backend": "upstream", "url": source, "index": index}
    monkeypatch.setattr("app.service.deliver", deliver)
    assert service.persist_completed_task(TASK)["data"] == [{"url": url} for url in SOURCES]
    assert service.store.task(TASK)["result_transfer"]["status"] == "ready"


def test_missing_legacy_cache_queries_only_original_two_clip_run(service, tmp_path, monkeypatch):
    legacy = [f"https://too.test/v1/assets/{TASK}/{index}?sig=fixture" for index in range(2)]
    service.store.update_task(TASK, {"result_urls": legacy})
    monkeypatch.setattr("app.service.result_path", lambda *args: tmp_path / "missing.mp3")
    client = Mock()
    client.poll.return_value = {"status": "succeeded", "result_urls": SOURCES}
    monkeypatch.setattr("app.service.ToonkitClient", lambda account: client)
    @contextmanager
    def lease(account):
        yield account
    monkeypatch.setattr(service.browser_runtime, "lease", lease)
    delivered = Mock(side_effect=lambda source, normalized, task_id, proxy, index, storage:
                     (metadata(index)["url"], metadata(index)))
    monkeypatch.setattr("app.service.deliver", delivered)
    assert len(service.persist_completed_task(TASK)["data"]) == 2
    client.poll.assert_called_once_with("original-run", '["clip-first","clip-second"]')
    client.submit.assert_not_called()
    assert [call.args[0] for call in delivered.call_args_list] == SOURCES
    assert service.store.task(TASK)["result_transfer"]["upstream_urls"] == SOURCES


def test_cached_legacy_outputs_never_need_account_browser(service, monkeypatch):
    service.store.update_task(TASK, {"result_urls": [f"https://too.test/v1/assets/{TASK}/{i}" for i in range(2)]})
    acquire = Mock(side_effect=AssertionError("browser must not open for cached outputs"))
    monkeypatch.setattr(service.browser_runtime, "acquire", acquire)
    monkeypatch.setattr("app.service.deliver", lambda source, normalized, task_id, proxy, index, storage:
                        (metadata(index)["url"], metadata(index)))
    assert len(service.persist_completed_task(TASK)["data"]) == 2
    acquire.assert_not_called()


def test_partial_original_run_recovery_does_not_drop_track_or_submit(service, tmp_path, monkeypatch):
    service.store.update_task(TASK, {"result_urls": [f"https://too.test/v1/assets/{TASK}/{i}" for i in range(2)]})
    monkeypatch.setattr("app.service.result_path", lambda *args: tmp_path / "missing.mp3")
    client = Mock()
    client.poll.return_value = {"status": "succeeded", "result_urls": [SOURCES[0]]}
    monkeypatch.setattr("app.service.ToonkitClient", lambda account: client)
    @contextmanager
    def lease(account):
        yield account
    monkeypatch.setattr(service.browser_runtime, "lease", lease)
    deliver = Mock()
    monkeypatch.setattr("app.service.deliver", deliver)
    assert service.persist_completed_task(TASK)["status"] == "running"
    task = service.store.task(TASK)
    assert len(task["result_transfer"]["upstream_urls"]) == 2
    assert task["status"] == "succeeded" and task["actual_cost"] == 15
    client.submit.assert_not_called()
    deliver.assert_not_called()


def test_expired_signatures_hidden_until_refreshed_and_saved(service, monkeypatch):
    old = [metadata(index, expired=True) for index in range(2)]
    service.store.update_task(TASK, {"result_urls": [item["url"] for item in old],
        "result_transfer": {"status": "ready", "upstream_urls": SOURCES, "items": old}})
    scheduled = Mock()
    monkeypatch.setattr(service, "_schedule_result_retry", scheduled)
    assert service.prepare_public_task(service.store.task(TASK))["data"] == []
    scheduled.assert_called_once()
    presign = Mock(side_effect=lambda item, settings: (item["url"] + "?fresh=1", {**metadata(item["index"]), "url": item["url"] + "?fresh=1"}))
    monkeypatch.setattr("app.service.presign", presign)
    monkeypatch.setattr("app.service.deliver", Mock(side_effect=AssertionError("do not upload existing S3 object")))
    result = service.persist_completed_task(TASK)
    assert [item["url"] for item in result["data"]] == [metadata(i)["url"] + "?fresh=1" for i in range(2)]
    assert service.store.task(TASK)["result_urls"] == [item["url"] for item in result["data"]]
    assert presign.call_count == 2


def test_poll_settles_generation_and_credits_before_independent_delivery(service, monkeypatch):
    service.store.update_task(TASK, {"status": "running", "result_urls": [], "balance_before": 20})
    client = Mock()
    client.poll.return_value = {"status": "succeeded", "result_urls": SOURCES}
    client.account_state.return_value = {"available_balance": 5}
    seen = []
    def schedule(task):
        seen.append(service.store.task(TASK))
        raise RuntimeError("scheduler temporarily unavailable")
    monkeypatch.setattr(service, "_schedule_result_retry", schedule)
    monkeypatch.setattr(service, "_deliver_completed", Mock(side_effect=AssertionError("delivery inside generation worker")))
    service._poll(TASK, client, time.monotonic() + 10)
    task = service.store.task(TASK)
    assert seen[0]["status"] == "succeeded" and seen[0]["actual_cost"] == 15
    assert task["status"] == "succeeded" and task["result_transfer"]["upstream_urls"] == SOURCES
    client.submit.assert_not_called()


def test_blocked_delivery_does_not_hold_generation_slots(service, monkeypatch):
    service._started = True
    service.store.update_task(TASK, {"status": "running", "result_urls": [], "balance_before": 20})
    client = Mock()
    client.poll.return_value = {"status": "succeeded", "result_urls": SOURCES}
    client.account_state.return_value = {"available_balance": 5}
    monkeypatch.setattr("app.service.ToonkitClient", lambda account: client)
    monkeypatch.setattr(service.browser_runtime, "acquire", lambda account: (account, False))
    entered, release = threading.Event(), threading.Event()
    def slow_deliver(task_id):
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(service, "_deliver_completed", slow_deliver)
    try:
        service._resume_poll(TASK)
        assert entered.wait(1)
        assert sum(service._active.values()) == 0 and service.store.active_count() == 0
        assert service._worker_slots.acquire(blocking=False)
        service._worker_slots.release()
    finally:
        release.set()


def test_retry_queue_deduplicates_and_obeys_backoff(service, monkeypatch):
    service._started = True
    service.store.update_task(TASK, {"result_transfer": {"status": "pending", "upstream_urls": SOURCES,
                                                     "next_retry_at": time.time() + 60}})
    queued = Mock()
    monkeypatch.setattr(service._result_pool, "submit", queued)
    service.retry_pending_results()
    queued.assert_not_called()
    service.store.update_task(TASK, {"result_transfer": {"status": "pending", "upstream_urls": SOURCES}})
    service.retry_pending_results()
    service.retry_pending_results()
    queued.assert_called_once()
    assert TASK in service._result_pending


def test_settings_mask_token_and_blank_patch_retains_existing_secret(service):
    result = service.update_settings(STORAGE)
    assert result["result_s3_center_token"] == "" and result["result_s3_center_token_configured"]
    service.update_settings({"result_s3_center_token": "   ", "task_workers": 3})
    assert service.settings()["result_s3_center_token"] == "fixture-token"
    with pytest.raises(ValueError):
        service.update_settings({"result_s3_center_api_base": "http://user:password@storage.test"})
    assert service.settings()["result_s3_center_api_base"] == STORAGE["result_s3_center_api_base"]


def test_unexpected_transfer_error_stores_class_not_secret(service, monkeypatch):
    monkeypatch.setattr("app.service.deliver", Mock(side_effect=RuntimeError("fixture-secret https://private.test/?token=secret")))
    assert service.persist_completed_task(TASK)["status"] == "running"
    transfer = service.store.task(TASK)["result_transfer"]
    assert transfer["last_error"] == "RuntimeError"
    assert transfer["attempts"] == 1 and transfer["next_retry_at"] > time.time()


@pytest.fixture
def api(service, tmp_path, monkeypatch):
    monkeypatch.setenv("TOO_API_KEY", "fixture-api")
    monkeypatch.setenv("TOO_ADMIN_TOKEN", "fixture-admin")
    monkeypatch.setenv("TOO_DATA_DIR", str(tmp_path))
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "store", service.store)
    monkeypatch.setattr(main, "service", service)
    client = TestClient(main.app)
    try:
        # Do not start account/browser maintenance in API projection tests.
        yield main, client
    finally:
        client.close()


def test_public_admin_and_content_hide_unready_urls_and_keep_two_ready_clips(api, service, monkeypatch):
    main, client = api
    headers = {"Authorization": "Bearer fixture-api"}
    admin = {"X-Admin-Token": "fixture-admin"}
    # Legacy rows remain unchanged until S3 is configured.
    before = service.store.task(TASK)
    for endpoint in (f"/v1/music/{TASK}", f"/v1/videos/{TASK}", f"/api/v3/contents/generations/tasks/{TASK}"):
        value = client.get(endpoint, headers=headers).json()
        assert value["status"] == "running" and value["progress"] == 95 and value["data"] == []
    for endpoint in ("/api/tasks", f"/api/tasks/{TASK}"):
        value = client.get(endpoint, headers=admin).json()
        task = value[0] if isinstance(value, list) else value
        assert task["status"] == "succeeded" and task["result_urls"] == []
        assert task["public_result"]["status"] == "running"
    assert client.get(f"/v1/music/{TASK}/content", headers=headers, follow_redirects=False).status_code == 409
    assert service.store.task(TASK) == before
    monkeypatch.setattr("app.service.deliver", lambda source, normalized, task_id, proxy, index, storage:
                        (metadata(index)["url"], metadata(index)))
    value = client.post(f"/api/tasks/{TASK}/persist-result", headers=admin).json()
    assert len(value["data"]) == 2 and value["status"] == "succeeded"
    for index in range(2):
        response = client.get(f"/v1/music/{TASK}/content?index={index}", headers=headers, follow_redirects=False)
        assert response.status_code == 307 and response.headers["location"] == metadata(index)["url"]
    assert client.get("/api/settings", headers=admin).json()["result_s3_center_token"] == ""


def test_synchronous_create_waits_for_delivery_public_state(api, service, monkeypatch):
    main, client = api
    monkeypatch.setattr(service, "create", lambda body: service.public_task(service.store.task(TASK)))
    sleeps = []
    def wait(seconds):
        sleeps.append(seconds)
        service.store.update_task(TASK, {"result_urls": [item["url"] for item in (metadata(0), metadata(1))],
            "result_transfer": {"status": "ready", "upstream_urls": SOURCES, "items": [metadata(0), metadata(1)]}})
    monkeypatch.setattr(main.time, "sleep", wait)
    response = client.post("/v1/music", headers={"Authorization": "Bearer fixture-api"},
                           json={"model": "suno-v6", "prompt": "fixture", "background": False})
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    assert len(response.json()["data"]) == 2 and sleeps == [2]


def test_legacy_asset_compatibility_redirects_to_s3(api, service):
    from app.result import signature
    _, client = api
    service.store.update_task(TASK, {"result_urls": [metadata(i)["url"] for i in range(2)],
        "result_transfer": {"status": "ready", "upstream_urls": SOURCES, "items": [metadata(0), metadata(1)]}})
    response = client.get(f"/v1/assets/{TASK}/1?sig={signature(TASK)}", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == metadata(1)["url"]
