"""Account-aware queue and durable Toonkit video task execution."""

from __future__ import annotations

import os
import logging
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .browser import BrowserError, BrowserRejected, browser_alive, start_managed_browser, stop_managed_browser
from .browser_runtime import BrowserRuntime
from .catalog import normalize_request
from .client import ToonkitClient, ToonkitError, account_media_lock
from .errors import public_error
from .media import MediaError
from .result import convert
from .store import Store


TERMINAL = {"succeeded", "failed", "expired"}
MAX_CREDIT_ACCOUNT_ATTEMPTS = 2
logger = logging.getLogger(__name__)


class AccountCreditRejected(Exception):
    """The upstream explicitly rejected this account after a terminal result."""


class AccountPoolUnavailable(Exception):
    """No enabled account can accept a new task before upstream submission."""


def _credit_rejected(message: str) -> bool:
    value = message.lower()
    return any(token in value for token in (
        "credit not enough", "credits not enough", "not enough credits",
        "insufficient credits", "insufficient balance", "credit balance", "not enough credit"))


def _estimate_cost(normalized: dict[str, Any], costs: list[dict[str, Any]]) -> float:
    """Use observed credits per second when exact media counts have no quote.

    The fallback is limited to observations with the same model, resolution,
    video count and audio count. It applies only when several durations show a
    stable rate, so image-count variants cannot invent a price from one task.
    """
    comparable = [item for item in costs if
                  item["model"] == normalized["model"] and
                  item["resolution"] == normalized["resolution"] and
                  item["video_count"] == len(normalized["videos"]) and
                  item["audio_count"] == len(normalized["audios"])]
    exact = [item for item in comparable if
             item["duration"] == normalized["duration"] and
             item["image_count"] == len(normalized["images"])]
    if exact:
        return max(float(item["cost"]) for item in exact)
    observations = [item for item in comparable if
                    float(item["cost"]) > 0 and int(item["duration"]) > 0]
    if len(observations) < 3 or len({item["duration"] for item in observations}) < 2:
        return 0.0
    rates = [float(item["cost"]) / int(item["duration"]) for item in observations]
    rate = statistics.median(rates)
    if any(abs(sample - rate) > rate * 0.05 for sample in rates):
        return 0.0
    return round(rate * int(normalized["duration"]), 2)


DEFAULT_SETTINGS: dict[str, Any] = {
    "task_workers": 10,
    "task_queue_capacity": 100,
    "poll_interval_seconds": 8,
    "task_timeout_seconds": 1800,
    "account_maintenance_interval_seconds": 300,
    "daily_checkin_enabled": False,
    "chrome_executable": os.getenv("TOO_CHROME_EXECUTABLE", "/usr/bin/chromium" if os.name != "nt" else "chrome.exe"),
    "chrome_user_data_root": os.getenv("TOO_CHROME_USER_DATA_ROOT", "/app/data/chrome-profiles"),
    "low_balance_disable_threshold": 0,
    "allow_video_reference_inputs": True,
    "excess_media_policy": "reject",
    "prompt_media_reference_cleanup_enabled": True,
}


class Service:
    def __init__(self, store: Store):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=50, thread_name_prefix="too-task")
        self._lock = threading.RLock()
        self._active: dict[int, int] = {}
        self._worker_slots = threading.BoundedSemaphore(50)
        self._started = False
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
        self.browser_runtime = BrowserRuntime(store, self.settings)
        self._browser_views: dict[int, tuple[dict[str, Any], bool, float]] = {}
        self._browser_views_lock = threading.RLock()

    def settings(self) -> dict[str, Any]:
        return {**DEFAULT_SETTINGS, **self.store.get_settings()}

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS)
        filtered = {key: value for key, value in values.items() if key in allowed}
        for key in ("task_workers", "task_queue_capacity", "poll_interval_seconds", "task_timeout_seconds"):
            if key in filtered and not 1 <= int(filtered[key]) <= (3600 if key != "task_queue_capacity" else 10000):
                raise ValueError(f"invalid {key}")
        self.store.update_settings(filtered)
        return self.settings()

    def start(self) -> None:
        self._started = True
        self._maintenance = threading.Thread(target=self._maintain_accounts, daemon=True,
                                             name="too-account-maintenance")
        self._maintenance.start()
        for task in reversed(self.store.tasks(1000)):
            if task["status"] == "queued":
                self._pool.submit(self._run, task["id"])
            elif task["status"] in {"submitted", "running"} and task["run_id"] and task["thread_id"]:
                self._pool.submit(self._resume_poll, task["id"])
            elif task["status"] == "preparing":
                if int(task["progress"]) < 30 and not task["run_id"]:
                    # Reference upload has not reached the generation RPC. It is
                    # safe to repeat after a restart, including an interrupted
                    # browser upload; the external task ID stays the same.
                    self.store.update_task(task["id"], {"status": "queued", "progress": 0,
                        "account_id": None, "error_code": "", "error_message": ""})
                    self._pool.submit(self._run, task["id"])
                else:
                    self.store.update_task(task["id"], {"status": "failed", "progress": 100,
                        "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                        "error_message": "service restarted during upstream submission; automatic resubmission is disabled",
                        "completed_at": time.time()})

    def stop(self) -> None:
        self._stop.set()
        if self._maintenance and self._maintenance.is_alive():
            self._maintenance.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=False)
        for account_id in list(self._browser_views):
            self.close_browser_view(account_id)
        self.browser_runtime.close_all_idle()

    def open_browser_view(self, account_id: int) -> dict[str, Any]:
        with self._browser_views_lock:
            if account_id in self._browser_views:
                account, owned, _ = self._browser_views[account_id]
                self._browser_views[account_id] = (account, owned, time.monotonic())
                return account
            account = self.store.account(account_id, private=True)
            if not account:
                raise KeyError(account_id)
            current, owned = self.browser_runtime.acquire(account)
            self._browser_views[account_id] = (current, owned, time.monotonic())
            return current

    def browser_view(self, account_id: int) -> dict[str, Any]:
        with self._browser_views_lock:
            if account_id not in self._browser_views:
                raise BrowserError("account browser view is closed")
            account, owned, _ = self._browser_views[account_id]
            self._browser_views[account_id] = (account, owned, time.monotonic())
            return account

    def close_browser_view(self, account_id: int) -> None:
        with self._browser_views_lock:
            entry = self._browser_views.pop(account_id, None)
        if entry:
            self.browser_runtime.release(entry[0], entry[1])

    def _maintain_accounts(self) -> None:
        while not self._stop.is_set():
            with self._browser_views_lock:
                expired = [account_id for account_id, (_, _, touched) in self._browser_views.items()
                           if time.monotonic() - touched > 20 * 60]
            for account_id in expired:
                self.close_browser_view(account_id)
            settings = self.settings()
            needs_rapid_recheck = False
            for account in self.store.accounts(private=True):
                if self._stop.is_set():
                    break
                if not account["enabled"] or not int(account["cdp_port"] or 0):
                    continue
                try:
                    checked = self.check_account(int(account["id"]))
                    if checked["status"] == "login_required":
                        needs_rapid_recheck = True
                    if checked["status"] == "active" and settings["daily_checkin_enabled"]:
                        self.checkin_account(int(account["id"]))
                except (BrowserError, ValueError, KeyError):
                    needs_rapid_recheck = True
                    continue
            interval = max(30, int(settings["account_maintenance_interval_seconds"]))
            # A restarted Chrome often exposes its tab before the app can make
            # authenticated requests. Retry that state promptly instead of
            # leaving a valid account unavailable for the full interval.
            self._stop.wait(min(15, interval) if needs_rapid_recheck else interval)

    def checkin_account(self, account_id: int) -> dict[str, Any]:
        # The observed claim endpoint grants signup credits once. It is not a
        # documented daily claim and must not be called on every maintenance pass.
        if not self.store.account(account_id):
            raise KeyError(account_id)
        return {"supported": False, "already_checked_in": False,
                "reason": "Toonkit exposes no verified daily credit claim endpoint"}

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_request(request)
        settings = self.settings()
        if not bool(settings["allow_video_reference_inputs"]) and normalized["videos"]:
            raise ValueError("video reference inputs are disabled by service settings")
        estimated_cost = _estimate_cost(normalized, self.store.model_costs())
        if not self._has_eligible_account(estimated_cost, set()):
            raise AccountPoolUnavailable("no available Toonkit account")
        if self.store.active_count() >= int(settings["task_workers"]) + int(settings["task_queue_capacity"]):
            raise RuntimeError("TOOAPI queue is full")
        task_id = "too_" + uuid.uuid4().hex[:16]
        task = self.store.create_task(task_id, request, normalized)
        if estimated_cost:
            task = self.store.update_task(task_id, {"estimated_cost": estimated_cost})
        self._pool.submit(self._run, task_id)
        return self.public_task(task)

    def _reserve_account(self, estimated_cost: float = 0, *,
                         exclude_ids: set[int] | None = None) -> dict[str, Any] | None:
        excluded = exclude_ids or set()
        with self._lock:
            if sum(self._active.values()) >= int(self.settings()["task_workers"]):
                return None
            accounts = [item for item in self.store.accounts(private=True)
                        if int(item["id"]) not in excluded and item["enabled"]
                        and item["status"] == "active" and item["proxy_url"]
                        and (item["balance"] is None or float(item["balance"]) >= estimated_cost)]
            accounts.sort(key=lambda item: (-int(item["priority"]), -(float(item["balance"] or 0)), item["id"]))
            for account in accounts:
                account_id = int(account["id"])
                active = self._active.get(account_id, 0)
                if active >= int(account["max_concurrency"]):
                    continue
                self._active[account_id] = active + 1
                return account
        return None

    def _has_eligible_account(self, estimated_cost: float, excluded: set[int]) -> bool:
        return any(int(item["id"]) not in excluded and item["enabled"]
                   and item["status"] == "active" and item["proxy_url"]
                   and (item["balance"] is None or float(item["balance"]) >= estimated_cost)
                   for item in self.store.accounts(private=True))

    def _release_account(self, account_id: int) -> None:
        with self._lock:
            self._active[account_id] = max(self._active.get(account_id, 1) - 1, 0)

    def check_account(self, account_id: int, *, start_browser: bool = False) -> dict[str, Any]:
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        account, owned = self.browser_runtime.acquire(account, force_managed=start_browser)
        try:
            return self._check_account_live(account)
        finally:
            self.browser_runtime.release(account, owned)

    def _check_account_live(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = int(account["id"])
        port = int(account["cdp_port"] or 0)
        if not browser_alive(port):
            return self.store.update_account(account_id, {"status": "login_required",
                                                         "last_error": "Toonkit account browser is unavailable"})
        # Chrome exposes a CDP page before Toonkit's JavaScript has finished
        # loading. After a service restart, one early fetch failure should not
        # put an otherwise valid account into login_required for an entire
        # maintenance interval. Complete HTTP rejections still fail promptly.
        for attempt in range(3):
            try:
                state = ToonkitClient(account).account_state()
                break
            except BrowserRejected as exc:
                return self.store.update_account(account_id, {"status": "login_required", "last_error": str(exc)[:300]})
            except BrowserError as exc:
                if attempt == 2:
                    return self.store.update_account(account_id, {"status": "login_required", "last_error": str(exc)[:300]})
                time.sleep(1)
        balance = state["available_balance"]
        project_id = ToonkitClient(account).browser.project_id()
        threshold = float(self.settings()["low_balance_disable_threshold"] or 0)
        values: dict[str, Any] = {"balance": balance, "balance_details": state["buckets"],
                                  "status": "active", "last_error": ""}
        if project_id:
            values["project_id"] = project_id
        if threshold > 0 and balance < threshold:
            values.update({"enabled": False, "status": "disabled_low_balance",
                           "last_error": f"credit balance below {threshold}"})
        return self.store.update_account(account_id, values)

    def _run(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or task["status"] != "queued":
            return
        estimated_cost = float(task.get("estimated_cost") or 0)
        if estimated_cost <= 0:
            estimated_cost = _estimate_cost(task["normalized"], self.store.model_costs())
            if estimated_cost > 0:
                self.store.update_task(task_id, {"estimated_cost": estimated_cost})
        attempted = list(task["attempted_account_ids"])
        credit_reason = task.get("error_message") or "Toonkit account has insufficient credits"
        unavailable_code = "INSUFFICIENT_CREDITS" if attempted else "ACCOUNT_POOL_UNAVAILABLE"
        unavailable_message = credit_reason if attempted else "no available Toonkit account"
        if not self._has_eligible_account(estimated_cost, set(attempted)):
            self._fail(task_id, unavailable_code, unavailable_message)
            return
        # The executor is only a waiter/dispatcher.  The configurable limit
        # applies to actual upstream work, not to the number of Python threads.
        while not self._stop.is_set():
            current = self.store.task(task_id)
            if not current or current["status"] != "queued":
                return
            if not self._has_eligible_account(estimated_cost, set(attempted)):
                self._fail(task_id, unavailable_code, unavailable_message)
                return
            if not self._worker_slots.acquire(timeout=1):
                continue
            with self._lock:
                if sum(self._active.values()) < int(self.settings()["task_workers"]):
                    break
            self._worker_slots.release()
            time.sleep(1)
        else:
            return
        try:
            while not self._stop.is_set():
                current = self.store.task(task_id)
                if not current or current["status"] != "queued":
                    return
                if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                    self._fail(task_id, "INSUFFICIENT_CREDITS", credit_reason)
                    return
                account = None
                account_deadline = time.monotonic() + int(self.settings()["task_timeout_seconds"])
                while not self._stop.is_set() and time.monotonic() < account_deadline:
                    account = self._reserve_account(estimated_cost, exclude_ids=set(attempted))
                    if account or not self._has_eligible_account(
                            estimated_cost, set(attempted)):
                        break
                    self._stop.wait(1)
                if not account:
                    code = "INSUFFICIENT_CREDITS" if attempted else "ACCOUNT_POOL_UNAVAILABLE"
                    message = credit_reason if attempted else "no available Toonkit account"
                    self._fail(task_id, code, message)
                    return
                account_id = int(account["id"])
                owned = False
                try:
                    with account_media_lock(account_id):
                        if self._stop.is_set():
                            return
                        self.store.update_task(task_id, {"account_id": account_id,
                            "status": "preparing", "progress": 5, "balance_before": None,
                            "run_id": "", "thread_id": "", "error_code": "", "error_message": ""})
                        account, owned = self.browser_runtime.acquire(account)
                        client = ToonkitClient(account, store=self.store)
                        # A just-started Chrome can expose CDP before the app is ready.
                        for attempt in range(12):
                            try:
                                before = client.account_state()["available_balance"]
                                break
                            except BrowserError as exc:
                                if "JavaScript request failed" not in str(exc) or attempt == 11:
                                    raise
                                if self._stop.wait(2):
                                    return
                        self.store.update_account(account_id, {"balance": before, "status": "active"})
                        self.store.update_task(task_id, {"balance_before": before, "progress": 12})
                        assets = client.upload_inputs(task["normalized"])
                        self.store.update_task(task_id, {"progress": 30})
                        attempted.append(account_id)
                        self.store.update_task(task_id, {"attempted_account_ids": attempted})
                        try:
                            submitted = client.submit(task["normalized"], assets)
                        except BrowserRejected as exc:
                            if not _credit_rejected(str(exc)):
                                self._fail(task_id, "UPSTREAM_REJECTED", str(exc))
                                return
                            raise AccountCreditRejected(str(exc)) from exc
                        except BrowserError as exc:
                            self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                                       str(exc) + "; automatic resubmission is disabled")
                            return
                        self.store.update_task(task_id, {"status": "submitted", "progress": 40,
                                                      "run_id": submitted["run_id"],
                                                      "thread_id": submitted["thread_id"]})
                    self._poll(task_id, client,
                               time.monotonic() + int(self.settings()["task_timeout_seconds"]))
                    return
                except AccountCreditRejected as exc:
                    credit_reason = str(exc)
                    self.store.update_account(account_id, {"last_error": credit_reason[:300]})
                    logger.warning("PIP task %s credit rejection on account %s (attempt %s/%s)",
                                   task_id, account_id, len(attempted), MAX_CREDIT_ACCOUNT_ATTEMPTS)
                    if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                        self._fail(task_id, "INSUFFICIENT_CREDITS", credit_reason)
                        return
                    self.store.update_task(task_id, {"status": "queued", "progress": 0,
                        "account_id": None, "run_id": "", "thread_id": "",
                        "balance_before": None, "error_code": "INSUFFICIENT_CREDITS",
                        "error_message": credit_reason})
                    continue
                except (BrowserError, ToonkitError, MediaError, ValueError) as exc:
                    self._fail(task_id, type(exc).__name__.upper(), str(exc))
                    return
                except Exception as exc:
                    self._fail(task_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
                    return
                finally:
                    self.browser_runtime.release(account, owned)
                    self._release_account(account_id)
        finally:
            self._worker_slots.release()

    def _resume_poll(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or not task["account_id"]:
            return
        account = self.store.account(task["account_id"], private=True)
        if not account:
            self._fail(task_id, "ACCOUNT_MISSING", "task account was removed")
            return
        self._worker_slots.acquire()
        with self._lock:
            self._active[int(task["account_id"])] = self._active.get(int(task["account_id"]), 0) + 1
        owned = False
        retry_on_other_account = False
        try:
            account, owned = self.browser_runtime.acquire(account)
            self._poll(task_id, ToonkitClient(account),
                       time.monotonic() + int(self.settings()["task_timeout_seconds"]))
        except AccountCreditRejected as exc:
            attempted = list(task["attempted_account_ids"])
            if int(task["account_id"]) not in attempted:
                attempted.append(int(task["account_id"]))
            reason = str(exc)
            self.store.update_account(int(task["account_id"]), {"last_error": reason[:300]})
            if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                self._fail(task_id, "INSUFFICIENT_CREDITS", reason)
            else:
                self.store.update_task(task_id, {"status": "queued", "progress": 0,
                    "account_id": None, "run_id": "", "thread_id": "",
                    "attempted_account_ids": attempted, "balance_before": None,
                    "error_code": "INSUFFICIENT_CREDITS", "error_message": reason})
                retry_on_other_account = True
        except Exception as exc:
            self._fail(task_id, type(exc).__name__.upper(), str(exc))
        finally:
            self.browser_runtime.release(account, owned)
            self._release_account(int(task["account_id"]))
            self._worker_slots.release()
        if retry_on_other_account and not self._stop.is_set():
            self._pool.submit(self._run, task_id)

    def resume_accepted_task(self, task_id: str) -> dict[str, Any]:
        """Resume polling an accepted task after an account login interruption."""
        with self._lock:
            task = self.store.task(task_id)
            if not task or not task["run_id"] or not task["thread_id"]:
                raise ValueError("task has no accepted upstream generation")
            if task["status"] != "failed" or "HTTP 401" not in (task["error_message"] or ""):
                raise ValueError("only a task stopped by account login can resume")
            account = self.store.account(int(task["account_id"]), private=True)
            if not account or account["status"] != "active":
                raise ValueError("log in and verify the task account before resuming")
            task = self.store.update_task(task_id, {"status": "running", "progress": 45,
                "error_code": "", "error_message": "", "completed_at": None})
            self._pool.submit(self._resume_poll, task_id)
            return self.public_task(task)

    def _poll(self, task_id: str, client: ToonkitClient, deadline: float) -> None:
        errors = 0
        conversion_errors = 0
        while time.monotonic() < deadline and not self._stop.is_set():
            task = self.store.task(task_id)
            if not task or task["status"] in TERMINAL:
                return
            try:
                state = client.poll(task["run_id"], task["thread_id"])
                errors = 0
            except BrowserRejected as exc:
                if "HTTP 401" in str(exc) or "LOGIN_REQUIRED" in str(exc):
                    self.store.update_account(int(task["account_id"]),
                                              {"status": "login_required", "last_error": "Toonkit login required"})
                    self._stop.wait(30)
                    continue
                errors += 1
                if errors >= 8:
                    raise
                time.sleep(min(60, 2 ** errors))
                continue
            except (BrowserError, ToonkitError):
                errors += 1
                if errors >= 8:
                    raise
                time.sleep(min(60, 2 ** errors))
                continue
            if state["status"] == "succeeded":
                if not state["result_urls"]:
                    errors += 1
                    if errors < 4:
                        time.sleep(3)
                        continue
                    self._fail(task_id, "RESULT_MISSING", "Toonkit completed without a media URL")
                    return
                urls = state["result_urls"]
                if task["normalized"].get("output_resolution"):
                    try:
                        urls = [convert(urls[0], task["normalized"], task_id,
                                        str(client.account.get("proxy_url") or ""))]
                    except Exception as exc:
                        conversion_errors += 1
                        if conversion_errors < 4:
                            self._stop.wait(10)
                            continue
                        self._fail(task_id, "RESULT_CONVERSION_FAILED", str(exc))
                        return
                after = None
                try:
                    after = client.account_state()["available_balance"]
                    self.store.update_account(int(task["account_id"]), {"balance": after})
                except BrowserError:
                    pass
                before = task.get("balance_before")
                finished_at = time.time()
                isolated = not self.store.has_account_overlap(
                    int(task["account_id"]), task_id, task["created_at"], finished_at)
                self.store.update_task(task_id, {"status": "succeeded", "progress": 100,
                    "result_urls": urls, "balance_after": after,
                    "actual_cost": max(0, float(before) - float(after))
                        if isolated and before is not None and after is not None else None,
                    "completed_at": finished_at})
                return
            if state["status"] == "failed":
                reason = state["error"] or "Toonkit generation failed"
                if _credit_rejected(reason):
                    raise AccountCreditRejected(reason)
                upstream_code = state.get("error_code") or ""
                if upstream_code in {"12009", "12011"}:
                    code = "CONTENT_MODERATION_FAILED"
                elif "video editing task" in reason.lower():
                    code = "VIDEO_EDIT_CONSTRAINT"
                elif (upstream_code == "10001" and
                      "agent callback execute failed: submit generatevideo failed" in reason.lower()):
                    # The agent run is terminal, but its callback does not
                    # establish whether a video job was accepted. Never
                    # resubmit automatically on this ambiguous boundary.
                    code = "UPSTREAM_TEMPORARY"
                elif upstream_code == "99999":
                    code = "UPSTREAM_TEMPORARY"
                else:
                    code = "UPSTREAM_FAILED"
                self._fail(task_id, code, reason)
                return
            self.store.update_task(task_id, {"status": "running", "progress": max(45, task["progress"])})
            time.sleep(max(2, int(self.settings()["poll_interval_seconds"])))
        if not self._stop.is_set():
            self.store.update_task(task_id, {"status": "expired", "progress": 100,
                "error_code": "UPSTREAM_TIMEOUT", "error_message": "Toonkit task timed out; upstream run may continue",
                "completed_at": time.time()})

    def _fail(self, task_id: str, code: str, message: str) -> None:
        self.store.update_task(task_id, {"status": "failed", "progress": 100,
            "error_code": code, "error_message": message[:600], "completed_at": time.time()})

    @staticmethod
    def public_task(task: dict[str, Any]) -> dict[str, Any]:
        result = {"id": task["id"], "object": task["normalized"].get("kind", "video"), "created_at": task["created_at"],
                  "status": task["status"], "model": task["model"], "progress": task["progress"],
                  "data": [{"url": url} for url in task["result_urls"]],
                  "account_id": task.get("account_id")}
        if task["status"] in {"failed", "expired"}:
            result["error"] = public_error(task["error_code"], task["error_message"])
        return result
