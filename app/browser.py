"""Use an authenticated native Chromium tab as the Toonkit Web API transport."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

import websocket


BASE = "https://toonkit.io"
_PROCESS_LOCK = threading.RLock()
_PROCESSES: dict[int, subprocess.Popen[Any]] = {}
_UPLOAD_LOCKS: dict[int, threading.RLock] = {}
_UPLOAD_TAB_URL = BASE + "/en/playground#too2api-upload"


class BrowserError(Exception):
    pass


class BrowserRejected(BrowserError):
    """A complete HTTP response established that the upstream rejected a call."""


def account_proxy_url(account: dict[str, Any]) -> str:
    value = str(account.get("proxy_url") or "").strip()
    parsed = urllib.parse.urlsplit(value)
    try:
        valid = parsed.scheme in {"socks5", "http", "https"} and bool(parsed.hostname) and bool(parsed.port)
    except ValueError:
        valid = False
    if not valid or parsed.path or parsed.query or parsed.fragment:
        raise BrowserError("account proxy must be a SOCKS5 or HTTP proxy with host and port")
    return value


def _targets(port: int) -> list[dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json", timeout=4) as response:
            return json.load(response)
    except Exception as exc:
        raise BrowserError(f"Toonkit CDP port {port} is unavailable: {type(exc).__name__}") from exc


def _page(port: int) -> dict[str, Any]:
    pages = [target for target in _targets(port) if target.get("type") == "page" and
             urllib.parse.urlsplit(target.get("url") or "").hostname == "toonkit.io"]
    pages.sort(key=lambda target: int("/playground" in str(target.get("url") or "")), reverse=True)
    if pages:
        return pages[0]
    raise BrowserError("open and log in to toonkit.io in the account browser")


def _upload_page(port: int) -> dict[str, Any]:
    """Use a home composer tab without moving the account's current project tab."""
    pages = [item for item in _targets(port) if item.get("type") == "page" and
             urllib.parse.urlsplit(item.get("url") or "").hostname == "toonkit.io"]
    home = [item for item in pages if "/playground" in urllib.parse.urlsplit(item.get("url") or "").path]
    if home:
        home.sort(key=lambda item: urllib.parse.urlsplit(item.get("url") or "").fragment != "too2api-upload")
        return home[0]
    # Target.createTarget is issued through the existing page so its cookies and
    # account proxy remain the same. A separate tab preserves an open project.
    source = _page(port)
    ws = websocket.create_connection(source["webSocketDebuggerUrl"], timeout=10,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                            "params": {"url": _UPLOAD_TAB_URL, "background": True}}))
        while True:
            reply = json.loads(ws.recv())
            if reply.get("id") == 1:
                if reply.get("error") or not (reply.get("result") or {}).get("targetId"):
                    raise BrowserError("Toonkit upload home tab could not be opened")
                target_id = reply["result"]["targetId"]
                break
    except (OSError, websocket.WebSocketException) as exc:
        raise BrowserError("Toonkit upload home tab could not be opened") from exc
    finally:
        ws.close()
    until = time.monotonic() + 20
    while time.monotonic() < until:
        target = next((item for item in _targets(port) if item.get("id") == target_id), None)
        if target and target.get("webSocketDebuggerUrl"):
            return target
        time.sleep(.2)
    raise BrowserError("Toonkit upload home tab did not become available")


def browser_alive(port: int) -> bool:
    try:
        _page(port)
        return True
    except BrowserError:
        return False


def _remove_stale_profile_locks(profile: Path) -> None:
    """Discard Chromium singleton links left by a stopped container."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        link = profile / name
        if link.is_symlink():
            link.unlink()


def start_managed_browser(account: dict[str, Any], *, chrome: str, profiles: Path) -> int:
    account_id = int(account["id"])
    port = 19300 + account_id
    profile = profiles / str(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    args = [chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--window-size=1280,960"]
    if os.name != "nt":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    proxy_url = account_proxy_url(account)
    args.append("--proxy-server=" + proxy_url)
    args.append(BASE + "/en/playground")
    with _PROCESS_LOCK:
        if browser_alive(port):
            return port
        existing = _PROCESSES.get(account_id)
        if existing and existing.poll() is None:
            existing.terminate()
            try:
                existing.wait(timeout=4)
            except subprocess.TimeoutExpired:
                existing.kill()
                existing.wait(timeout=4)
        _remove_stale_profile_locks(profile)
        _PROCESSES[account_id] = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL,
                                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if browser_alive(port):
                return port
            time.sleep(.1)
        try:
            pages = [item for item in _targets(port) if item.get("type") == "page"]
            hosts = {urllib.parse.urlsplit(str(item.get("url") or "")).hostname for item in pages}
            redirected = sorted(host for host in hosts if host and host != "toonkit.io")
            if redirected:
                raise BrowserError(f"Toonkit browser reached {redirected[0]}; configure an account proxy")
        except BrowserError as exc:
            if "configure an account proxy" in str(exc):
                raise
    raise BrowserError("managed Toonkit Chrome did not start")


def close_browser_port(port: int) -> None:
    """Ask a local CDP browser to exit, including one orphaned by a restart."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            endpoint = json.load(response).get("webSocketDebuggerUrl")
        if endpoint:
            ws = websocket.create_connection(endpoint, timeout=2, suppress_origin=True)
            try:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            finally:
                ws.close()
    except (OSError, ValueError, KeyError, websocket.WebSocketException):
        pass


def stop_managed_browser(account_id: int) -> None:
    with _PROCESS_LOCK:
        process = _PROCESSES.pop(account_id, None)
    close_browser_port(19300 + account_id)
    if process and process.poll() is None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def clear_managed_profile(account: dict[str, Any], profiles: Path) -> None:
    """Delete only the service-owned profile for this numeric account ID."""
    account_id = int(account["id"])
    port = int(account.get("cdp_port") or 0)
    if port == 19300 + account_id and browser_alive(port):
        try:
            CDPBrowser(port).command("Browser.close")
        except BrowserError:
            pass
        for _ in range(30):
            if not browser_alive(port):
                break
            time.sleep(.1)
        if browser_alive(port):
            raise BrowserError("managed Chrome is still running; profile was not removed")
    stop_managed_browser(account_id)
    root = profiles.resolve()
    profile = (root / str(account_id)).resolve()
    if profile.parent != root:
        raise BrowserError("profile path is outside the configured profile root")
    if profile.exists():
        shutil.rmtree(profile)


class CDPBrowser:
    def __init__(self, port: int, timeout: float = 90) -> None:
        self.port = int(port)
        self.timeout = timeout

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        target = _page(self.port)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=self.timeout,
                                         suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                result = json.loads(ws.recv())
                if result.get("id") != 1:
                    continue
                if "error" in result:
                    raise BrowserError(f"CDP {method}: {result['error'].get('message', 'unknown error')}")
                return result.get("result") or {}
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"CDP {method} failed: {type(exc).__name__}") from exc
        finally:
            ws.close()

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.command("Runtime.evaluate", {"expression": expression,
                                                   "awaitPromise": await_promise,
                                                   "returnByValue": True})
        if "exceptionDetails" in result:
            raise BrowserError("Toonkit browser JavaScript request failed")
        value = result.get("result") or {}
        if value.get("subtype") == "error":
            raise BrowserError("Toonkit browser JavaScript request returned an error")
        return value.get("value")

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith("/api/") or path.startswith("//"):
            raise ValueError("Toonkit API path must be same-origin")
        payload = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
        expression = (
            "(async()=>{const s=await(await fetch('/api/auth/session',{credentials:'include'})).json();"
            "if(!s?.accessToken)return JSON.stringify({http:401,body:{code:'LOGIN_REQUIRED',message:'Toonkit login required'}});"
            "const r=await fetch(" + json.dumps(path) + ",{method:" + json.dumps(method) + ","
            "credentials:'include',headers:{'authorization':'Bearer '+s.accessToken,"
            "'content-type':'application/json'},"
            + ("body:JSON.stringify(" + payload + ")," if body is not None else "") +
            "});let d;try{d=await r.json()}catch{d={}};"
            "return JSON.stringify({http:r.status,body:d})})()"
        )
        value = self.evaluate(expression, await_promise=True)
        try:
            response = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise BrowserError("Toonkit returned unreadable JSON") from exc
        status = int(response.get("http") or 0)
        if status >= 400:
            detail = response.get("body") or {}
            code = str(detail.get("code") or detail.get("errorCode") or status)
            message = str(detail.get("message") or detail.get("error") or "request rejected")[:300]
            raise BrowserRejected(f"Toonkit HTTP {status} {code}: {message}")
        data = response.get("body")
        if not isinstance(data, dict):
            raise BrowserError("Toonkit returned an invalid response")
        return data

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, body)

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def media_token(self) -> str:
        value = self.evaluate("(async()=>{const s=await(await fetch('/api/auth/session',{credentials:'include'})).json();return s.ncpToken||''})()", await_promise=True)
        return str(value or "")

    def screenshot(self) -> str:
        result = self.command("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        if not result.get("data"):
            raise BrowserError("Toonkit Chrome returned no screenshot")
        return "data:image/png;base64," + result["data"]

    def click(self, x: float, y: float) -> None:
        for count, kind in enumerate(("mousePressed", "mouseReleased"), 1):
            self.command("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                                      "button": "left", "clickCount": 1})

    def scroll(self, delta_y: float) -> None:
        self.command("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 700, "y": 500,
                                                  "deltaX": 0, "deltaY": delta_y})

    def cookies(self) -> list[dict[str, Any]]:
        return self.command("Network.getCookies", {"urls": [BASE + "/"]}).get("cookies") or []

    def project_id(self) -> str:
        value = self.evaluate("new URL(location.href).searchParams.get('projectId')||''")
        return str(value or "")

    def ensure_project_id(self) -> str:
        """Open an empty Canvas project from the logged-in home page if needed."""
        project_id = self.project_id()
        if project_id.isdigit():
            return project_id
        path = self.evaluate("location.pathname")
        if path == "/home":
            opened = self.evaluate("""(()=>{
              const button = Array.from(document.querySelectorAll(
                'button[data-slot="sidebar-menu-button"]')).find(item =>
                  /^(Canvas|画布)$/.test((item.innerText || '').trim()));
              if (!button || button.disabled) return false;
              button.click();
              return true;
            })()""")
            if not opened:
                raise BrowserError("open a Toonkit video project in the account browser")
        elif path != "/nest-agent-project":
            raise BrowserError("open a Toonkit video project in the account browser")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            project_id = self.project_id()
            if project_id.isdigit():
                return project_id
            time.sleep(.5)
        raise BrowserError("Toonkit video project did not finish opening")

    def user_info(self) -> dict[str, str]:
        """Read the logged-in web identity injected by Toonkit into its page."""
        result = self.evaluate("""(()=>{let d=JSON.parse(window.__businessUserInfoStringify||'{}').data||{};
          return {consumer_uid:String(d.consumer_user?.user_info?.user_id||''),
            workspace_id:String(d.space_info?.workspace_id||''),
            space_id:String(d.space_info?.space_id||''), app_id:'573081'}})()""")
        if not isinstance(result, dict) or not all(result.values()):
            raise BrowserError("Toonkit login identity is not available in the browser")
        return result

    def upload_file(self, path: Path) -> dict[str, Any]:
        """Upload through the logged-in web composer's real file input."""
        if not path.is_file():
            raise BrowserError("local upload file is missing")
        with _PROCESS_LOCK:
            lock = _UPLOAD_LOCKS.setdefault(self.port, threading.RLock())
        with lock:
            return self._upload_file_locked(path)

    def _upload_file_locked(self, path: Path) -> dict[str, Any]:
        target = _upload_page(self.port)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=90,
                                         suppress_origin=True)
        sequence = 0
        pending: deque[dict[str, Any]] = deque()

        def command(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal sequence
            ws.settimeout(90)
            sequence += 1
            current = sequence
            ws.send(json.dumps({"id": current, "method": method, "params": params or {}}))
            while True:
                event = json.loads(ws.recv())
                if event.get("id") == current:
                    if event.get("error"):
                        raise BrowserError(f"CDP {method}: {event['error'].get('message', 'failed')}")
                    return event.get("result") or {}
                pending.append(event)

        def evaluate(expression: str) -> Any:
            return command("Runtime.evaluate", {"expression": expression,
                "returnByValue": True}).get("result", {}).get("value")

        def click(point: dict[str, float]) -> None:
            for kind in ("mousePressed", "mouseReleased"):
                command("Input.dispatchMouseEvent", {"type": kind, "x": point["x"],
                    "y": point["y"], "button": "left", "clickCount": 1})

        def clear_composer_attachments() -> None:
            count_js = "document.querySelectorAll('[data-too2api-upload-composer=\"1\"] [data-slot=\"attachment-preview-media-shell\"]').length"
            click_js = "(()=>{const x=document.querySelector('[data-too2api-upload-composer=\"1\"] [data-slot=\"attachment-preview-remove-button\"]');x?.click();return !!x})()"
            for _ in range(50):
                count = int(evaluate(count_js) or 0)
                if count == 0:
                    return
                if not evaluate(click_js):
                    raise BrowserError("Toonkit composer attachment cannot be removed")
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    if int(evaluate(count_js) or 0) < count:
                        break
                    time.sleep(.1)
                else:
                    raise BrowserError("Toonkit composer attachment did not clear")
            raise BrowserError("Toonkit composer has too many stale attachments")

        create_body: dict[str, Any] | None = None
        create_result: dict[str, Any] | None = None
        upload_phases = {"prepare": 0, "chunks": 0, "cloud_asset": 0, "registration": 0,
                         "network_failed": 0}
        upload_failures: list[str] = []
        request_phases: dict[str, str] = {}
        intercept_chooser = False
        try:
            command("Page.enable")
            command("Network.enable")
            until_ready = time.monotonic() + 35
            while time.monotonic() < until_ready:
                ready = evaluate("""(()=>{document.querySelectorAll('[data-too2api-upload-composer]')
                  .forEach(x=>x.removeAttribute('data-too2api-upload-composer'));
                  const composer=[...document.querySelectorAll('[data-slot="prompt-composer"]')]
                    .find(x=>{const r=x.getBoundingClientRect();return r.width>0&&r.height>0&&
                      !!x.querySelector('[data-slot="prompt-composer-resource-runtime"] input[type="file"][multiple]')});
                  if(!composer)return false;
                  composer.setAttribute('data-too2api-upload-composer','1');return true})()""")
                if ready:
                    break
                time.sleep(.3)
            else:
                raise BrowserError("Toonkit home upload composer is unavailable")
            clear_composer_attachments()
            # Current Toonkit keeps its composer input mounted even when the upload
            # dropdown does not open. Setting this input dispatches the same change
            # event as the browser file picker and preserves the account's proxy.
            root = command("DOM.getDocument").get("root", {}).get("nodeId")
            node = command("DOM.querySelector", {"nodeId": root,
                "selector": '[data-too2api-upload-composer="1"] [data-slot="prompt-composer-resource-runtime"] input[type="file"][multiple]'})
            input_node_id = node.get("nodeId")
            if input_node_id:
                command("DOM.setFileInputFiles", {"files": [str(path.resolve())],
                        "nodeId": input_node_id})
            else:
                # Older composer versions expose the input only after opening a menu.
                command("Page.setInterceptFileChooserDialog", {"enabled": True})
                intercept_chooser = True
                menu_point = evaluate("""(()=>{let x=document.querySelector('[role=menuitem]');
                  let r=x?.getBoundingClientRect();return r?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()""")
                if not menu_point:
                    button_point = evaluate(r"""(()=>{let x=[...document.querySelectorAll('[aria-label]')]
                      .find(x=>x.getAttribute('aria-label')==='\u4e0a\u4f20\u53c2\u8003\u7d20\u6750')
                      || document.querySelector('button:has([data-slot="home-input-add-icon"])');
                      let r=x?.getBoundingClientRect();return r?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()""")
                    if not button_point:
                        raise BrowserError("Toonkit reference upload button is unavailable")
                    click(button_point)
                    time.sleep(.2)
                    menu_point = evaluate("""(()=>{let x=document.querySelector('[role=menuitem]');
                      let r=x?.getBoundingClientRect();return r?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()""")
                chooser = next((item for item in pending
                                if item.get("method") == "Page.fileChooserOpened"), None)
                if chooser is None and menu_point:
                    click(menu_point)
                if chooser is None and not menu_point:
                    raise BrowserError("Toonkit upload input and menu are unavailable")
                until = time.monotonic() + 8
                while chooser is None and time.monotonic() < until:
                    item = json.loads(ws.recv())
                    pending.append(item)
                    if item.get("method") == "Page.fileChooserOpened":
                        chooser = item
                if not chooser or not chooser.get("params", {}).get("backendNodeId"):
                    raise BrowserError("Toonkit did not open the expected file chooser")
                command("DOM.setFileInputFiles", {"files": [str(path.resolve())],
                        "backendNodeId": chooser["params"]["backendNodeId"]})
            tracked: dict[str, str] = {}
            # Large image references can take longer than 90 seconds through
            # the account proxy. Keep the wait bounded by the task timeout.
            megabytes = (path.stat().st_size + (1 << 20) - 1) // (1 << 20)
            until = time.monotonic() + min(240, max(90, 60 + megabytes * 8))
            idle_until = time.monotonic() + max(45, min(75, 30 + megabytes * 2))
            upload_started = False
            while time.monotonic() < until and (upload_started or time.monotonic() < idle_until) and create_result is None:
                if pending:
                    item = pending.popleft()
                else:
                    ws.settimeout(min(5, max(0.1, until - time.monotonic())))
                    try:
                        item = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        continue
                method = item.get("method")
                params = item.get("params") or {}
                request_id = str(params.get("requestId") or "")
                if method == "Network.requestWillBeSent":
                    request = params.get("request") or {}
                    route = urllib.parse.urlsplit(str(request.get("url") or "")).path
                    if route.endswith("/asset/prepare_upload_cloud"):
                        request_phases[request_id] = "prepare"
                        upload_started = True
                    elif route.startswith("/upload/v1/"):
                        request_phases[request_id] = "chunk"
                        upload_started = True
                    elif route.endswith("/asset/create_cloud_asset"):
                        request_phases[request_id] = "cloud_asset"
                        upload_started = True
                    if route.endswith("/asset/create_v2"):
                        request_phases[request_id] = "registration"
                        upload_started = True
                        raw = request.get("postData")
                        if raw:
                            try:
                                create_body = json.loads(raw)
                            except ValueError:
                                pass
                elif method == "Network.responseReceived":
                    response = params.get("response") or {}
                    route = urllib.parse.urlsplit(str(response.get("url") or "")).path
                    phase = request_phases.get(request_id)
                    status = int(response.get("status") or 0)
                    if phase and status >= 400 and len(upload_failures) < 4:
                        upload_failures.append(f"{phase}:HTTP {status}")
                    if route.endswith("/asset/prepare_upload_cloud"):
                        upload_phases["prepare"] += 1
                    elif route.startswith("/upload/v1/"):
                        upload_phases["chunks"] += 1
                    elif route.endswith("/asset/create_cloud_asset"):
                        upload_phases["cloud_asset"] += 1
                    if route.endswith("/asset/create_v2"):
                        upload_phases["registration"] += 1
                        tracked[request_id] = route
                elif method == "Network.loadingFailed":
                    upload_phases["network_failed"] += 1
                    phase = request_phases.get(request_id)
                    if phase and len(upload_failures) < 4:
                        error = str(params.get("errorText") or "network failed")
                        upload_failures.append(f"{phase}:{error[:80]}")
                elif method == "Network.loadingFinished" and request_id in tracked:
                    result = command("Network.getResponseBody", {"requestId": request_id})
                    raw = result.get("body") or "{}"
                    if result.get("base64Encoded"):
                        raw = base64.b64decode(raw).decode("utf-8", "replace")
                    try:
                        create_result = json.loads(raw)
                    except ValueError as exc:
                        raise BrowserError("Toonkit asset creation returned invalid JSON") from exc
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"Toonkit browser upload failed: {type(exc).__name__}") from exc
        finally:
            try:
                clear_composer_attachments()
            except Exception:
                # A failed upload must retain its original error. The next upload
                # will try to clear any remaining composer attachments first.
                pass
            if intercept_chooser:
                try:
                    command("Page.setInterceptFileChooserDialog", {"enabled": False})
                except Exception:
                    pass
            ws.close()
        if create_result is None:
            phases = ", ".join(f"{key}={value}" for key, value in upload_phases.items())
            if not upload_started:
                phases += ", no upload request after file selection"
            if upload_failures:
                phases += ", failures=" + "; ".join(upload_failures)
            raise BrowserError(f"Toonkit asset creation did not complete ({phases})")
        if str(create_result.get("ret")) != "0" and create_body:
            # This registration RPC is idempotent by source asset ID.
            return self.post("/api/biz/v3/asset/create_v2", create_body)
        if str(create_result.get("ret")) != "0":
            raise BrowserRejected(f"Toonkit asset registration: {str(create_result.get('errmsg') or 'failed')[:200]}")
        return create_result.get("data") or {}
