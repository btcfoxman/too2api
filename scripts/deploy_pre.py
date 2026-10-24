"""Idle-only TOO release with source verification, online backup and pinned rollback."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

os.umask(0o077)
SERVICE = "too2api"
ROOT = None
M = None

class Runtime:
    SERVICES = {"too2api": "TOO"}

    def inspect(self, service):
        return json.loads(subprocess.check_output(["docker", "inspect", service], text=True))[0]

    def container_python(self, service, source):
        import base64
        encoded = base64.b64encode(source.encode()).decode()
        result = subprocess.run(["docker", "exec", service, "python", "-B", "-c",
                                 "import base64;exec(base64.b64decode(" + repr(encoded) + "))"],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError("private container probe failed")
        return result.stdout

    def compose(self, info, override):
        result = ["docker", "compose", "--project-directory", info["directory"], "-p", info["project"]]
        for path in info["configs"]:
            result += ["-f", path]
        return result + ["-f", str(override)]

r = Runtime()
TERMINAL = {"succeeded", "success", "completed", "complete", "done", "failed", "error", "cancelled", "canceled", "expired", "deleted", "timeout", "submission_unknown", "polling_paused"}


def save(name, value):
    target = ROOT / name
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.chmod(0o600)
    tmp.replace(target)


def command(args, label, timeout=90):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    (ROOT / (label + ".private.log")).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(label + " failed")
    return result.stdout


def python(source):
    return json.loads(r.container_python(SERVICE, source))


TASK_ROW_SNAPSHOT = '''
def task_snapshot_row(row):
    value = dict(row)
    # Adding the default empty delivery metadata does not alter an existing
    # generation. All original fields and non-empty delivery metadata remain
    # part of the strict preservation comparison.
    if value.get("result_transfer_json") == "{}":
        value.pop("result_transfer_json")
    return value
'''

SNAPSHOT = TASK_ROW_SNAPSHOT + '''import hashlib,json,os,pathlib,sqlite3
service=SERVICE
digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str).encode()).hexdigest()
base=pathlib.Path('/app')
hashes={str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (base/'app').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
directories={base/'data'}
directories.update(pathlib.Path(v) for k,v in os.environ.items() if k.endswith('_DATA_DIR') and v)
candidates={(directory/(service+extension)).resolve() for directory in directories for extension in ('.db','.sqlite','.sqlite3')}
for key,value in os.environ.items():
 if key.endswith(('_DATABASE_PATH','_DB_PATH')) and value:candidates.add(pathlib.Path(value).resolve())
databases=[]
for path in sorted(candidates):
 if not path.is_file():continue
 with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=5) as db:
  db.row_factory=sqlite3.Row
  tables={v[0] for v in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  if 'tasks' not in tables:continue
  db.execute('BEGIN')
  tasks={v['id']:digest(task_snapshot_row(v)) for v in db.execute('SELECT * FROM tasks ORDER BY id')}
  statuses=dict(db.execute('SELECT status,count(*) FROM tasks GROUP BY status'))
  settings={}
  for table in ('service_settings','runtime_settings','settings'):
   if table not in tables:continue
   rows=[dict(v) for v in db.execute('SELECT * FROM '+table)]
   for row in rows:
    if 'key' in row:
     key=row['key'];value=row.get('value',row.get('body'))
     if key=='account_balance_alert_state_v1':
      try:
       value=json.loads(value)
       if isinstance(value.get('last_refill'),dict):value['last_refill']['started_at']=None
      except (ValueError,TypeError):pass
     settings[table+':'+str(key)]=value
    else:settings[table]=rows
  manual={}
  if 'costref_config' in tables:
   for row in db.execute('SELECT kind,id,body FROM costref_config ORDER BY kind,id'):
    value=json.loads(row['body'])
    if value.get('origin') not in {'workbook','purchase_default'}:
     manual[row['kind']+':'+row['id']]=digest(value)
  account_statuses={};account_config={}
  reward_statuses={}
  if 'account_rewards' in tables:
   for reward in db.execute('SELECT body FROM account_rewards'):
    state=json.loads(reward[0]).get('status','')
    if state in {'claiming','submitting'}:reward_statuses[state]=reward_statuses.get(state,0)+1
  if 'accounts' in tables:
   cols={v[1] for v in db.execute('PRAGMA table_info(accounts)')}
   if 'status' in cols:account_statuses=dict(db.execute('SELECT status,count(*) FROM accounts GROUP BY status'))
   protected={'name','email','enabled','max_concurrency','concurrency','weight','proxy_url','proxy_id','min_balance','purchase_unit_price','region','expected_user_id','username','priority','group_id','model_map','pool'}
   for raw in db.execute('SELECT * FROM accounts ORDER BY id'):
    row=dict(raw)
    if 'body' in row:
     try:row={**json.loads(row['body']),**row}
     except (ValueError,TypeError):pass
    config={key:row[key] for key in protected if key in row}
    config['daily_rewards_enabled']=bool(row.get('daily_rewards_enabled',0))
    account_config[str(row['id'])]=digest(config)
  databases.append({'path':str(path),'task_hashes':tasks,'statuses':statuses,'settings':settings,'manual_cost_config':manual,'account_statuses':account_statuses,'account_config':account_config,'reward_statuses':reward_statuses})
print(json.dumps({'app_hashes':hashes,'databases':databases}))
'''.replace("SERVICE", repr(SERVICE))


def snapshot():
    return python(SNAPSHOT)


def busy(snap):
    result = {}
    for db in snap["databases"]:
        result.update({"reward_" + key: value for key, value in db.get("reward_statuses", {}).items() if value})
        result.update({key: value for key, value in db["statuses"].items() if str(key).lower() not in TERMINAL and value})
        result.update({"account_" + str(key): value for key, value in db["account_statuses"].items() if key in {"logging_in", "login_pending", "authorizing", "verifying"} and value})
    return result


def sources_guard():
    current = r.inspect(SERVICE)
    assert current["State"]["Running"] and current["Image"] == M["image_id"], "runtime image changed"
    snap = snapshot()
    assert snap["app_hashes"] == M["app_hashes"], "runtime source changed"
    assert len(snap["databases"]) == 1, "one task database required"
    for name, expected in M["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
    return snap


def signal_app(name, expected=None):
    # docker-init/xvfb-run may be PID 1; freeze the actual single ASGI process.
    source = '''import json,os,pathlib,signal
matches=[]
for path in pathlib.Path('/proc').iterdir():
 if not path.name.isdigit():continue
 try:
  args=(path/'cmdline').read_bytes().decode().split('\\0')
  executable=pathlib.Path(args[0]).name if args else ''
  if (executable.startswith('python') or executable=='uvicorn') and any(v=='uvicorn' or v.endswith('/uvicorn') for v in args) and any(v in {'main:app','app.main:app'} for v in args):matches.append(path)
 except (FileNotFoundError,PermissionError,ProcessLookupError):pass
assert len(matches)==1,'expected one application process'
root=matches[0]
identity={'pid':int(root.name),'start':root.joinpath('stat').read_text().split()[21]}
assert EXPECTED is None or identity==EXPECTED,'application process restarted'
os.kill(identity['pid'],getattr(signal,SIGNAL))
print(json.dumps(identity))
'''.replace("EXPECTED", repr(expected)).replace("SIGNAL", repr(name))
    return python(source)


def resume(identity):
    if identity is None:
        return
    try:
        state = r.inspect(SERVICE)
        if state["State"]["Running"] and state["Image"] == M["image_id"]:
            signal_app("SIGCONT", identity)
    except Exception:
        pass


def watchdog(identity):
    probe = "import os,pathlib,signal; pid=" + str(identity["pid"]) + "; assert (pathlib.Path('/proc')/str(pid)/'stat').read_text().split()[21]==" + repr(identity["start"]) + "; os.kill(pid,signal.SIGCONT)"
    code = "import time,subprocess,json\ntime.sleep(45)\n" + "try:\n item=json.loads(subprocess.check_output(['docker','inspect'," + repr(SERVICE) + "]))[0]\n if item['State']['Running'] and item['Image']==" + repr(M["image_id"]) + ":\n  subprocess.run(['docker','exec'," + repr(SERVICE) + ",'python','-c'," + repr(probe) + "],capture_output=True,timeout=10)\nexcept Exception:pass\n"
    subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def probe_request(path, *, mode="admin"):
    """Read only localhost using the host's existing admin authentication."""
    assert mode in {"admin", "sync", "none"}
    assert path.startswith("/") and not path.startswith("//")
    ports = r.inspect(SERVICE)["NetworkSettings"]["Ports"]
    port = next(int(name.split("/")[0]) for name, bindings in ports.items() if bindings)
    source = '''import os,json,urllib.request,urllib.error,hmac,hashlib,time
prefix=PREFIX
token=os.environ.get(prefix+('_COST_SYNC_TOKEN' if MODE=='sync' else '_ADMIN_TOKEN'),'')
if MODE=='admin' and not token:token=os.environ.get(prefix+'_API_KEY','')
headers={'Authorization':'Bearer '+token,'X-Admin-Token':token,'X-Requested-With':'art2api','Accept':'application/json'} if MODE!='none' else {}
if MODE=='admin':
 if prefix in {'MAG','POL'}:headers['Cookie']=prefix.lower()+'_admin='+token
 if prefix=='HIGG':headers['Cookie']='higg_admin='+hmac.new(token.encode(),b'higg2api-admin',hashlib.sha256).hexdigest()
 if prefix=='LINGYA':headers['Cookie']='lingya_admin_session='+hmac.new(token.encode(),b'lingya-admin-session-v1',hashlib.sha256).hexdigest()
 if prefix=='ART':
  expiry=str(int(time.time())+300)
  headers['Cookie']='art_session='+expiry+'.'+hmac.new(token.encode(),expiry.encode(),hashlib.sha256).hexdigest()
request=urllib.request.Request(URL,headers=headers,method='GET')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
 with opener.open(request,timeout=10) as response:
  raw=response.read().decode()
  result={'status':response.status,'data':json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else {}}
except urllib.error.HTTPError as error:result={'status':error.code}
print(json.dumps(result))
'''
    for name, value in {"PREFIX": repr(r.SERVICES[SERVICE]), "MODE": repr(mode), "URL": repr("http://127.0.0.1:" + str(port) + path)}.items():
        source = source.replace(name, value)
    return json.loads(r.container_python(SERVICE, source))


def health(path="/health", mode="none"):
    last_status, last_error = None, None
    for _ in range(30):
        assert r.inspect(SERVICE)["State"]["Running"], "application stopped"
        try:
            response = probe_request(path, mode=mode)
            last_status = response["status"]
            if response["status"] == 200:
                return response
        except Exception as error:
            last_error = type(error).__name__
        time.sleep(2)
    raise RuntimeError("service health timeout: status=" + str(last_status) + ", error=" + str(last_error))


def deploy():
    initial = sources_guard()
    if busy(initial):
        save("status.json", {"phase": "deferred", "service": SERVICE, "busy": busy(initial)})
        return
    built = json.loads((ROOT / "build.json").read_text())
    old = r.inspect(SERVICE)
    info = {"project": M["project"], "directory": M["directory"], "configs": M["configs"]}
    backup = ROOT / "backups"
    backup.mkdir(exist_ok=True, mode=0o700)
    config_paths = list(dict.fromkeys([str(Path(M["directory"]) / ".env"), *M["configs"]]))
    config_hashes = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in config_paths}
    for index, name in enumerate(config_paths):
        (backup / (str(index) + "-" + Path(name).name)).write_bytes(Path(name).read_bytes())
    save("compose.json", {"services": {SERVICE: {"image": built["image"]}}})
    assert command(["docker", "image", "inspect", "--format", "{{.Id}}", built["base_image"]], "verify-rollback-image").strip() == M["image_id"]
    assert command(["docker", "image", "inspect", "--format", "{{.Id}}", built["image"]], "verify-candidate-image").strip() == built["image_id"]
    save("rollback.compose.json", {"services": {SERVICE: {"image": built["base_image"]}}})
    start = r.compose(info, ROOT / "compose.json") + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", SERVICE]
    restore = r.compose(info, ROOT / "rollback.compose.json") + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", SERVICE]
    identity, stopped = None, False
    try:
        # Online backup first. Large databases must never spend the brief
        # application freeze waiting on backup/check/copy work.
        path = initial["databases"][0]["path"]
        temporary = "/tmp/" + M["release"] + "-" + SERVICE + ".db"
        r.container_python(SERVICE, "import sqlite3,os\nwith sqlite3.connect('file:'+" + repr(path) + "+'?mode=ro',uri=True) as src,sqlite3.connect(" + repr(temporary) + ") as dst:\n src.backup(dst)\n assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'\nos.chmod(" + repr(temporary) + ",0o600)")
        command(["docker", "cp", SERVICE + ":" + temporary, str(backup / "tasks.db")], "backup", timeout=120)
        r.container_python(SERVICE, "from pathlib import Path\nPath(" + repr(temporary) + ").unlink()")
        identity = signal_app("SIGSTOP")
        watchdog(identity)
        frozen_at = time.monotonic()
        cutover = snapshot()
        if busy(cutover):
            save("status.json", {"phase": "deferred", "service": SERVICE, "busy": busy(cutover)})
            return
        assert cutover["app_hashes"] == M["app_hashes"], "source changed before cutover"
        frozen_container = r.inspect(SERVICE)
        assert frozen_container["Id"] == old["Id"] and frozen_container["Image"] == M["image_id"], "container changed before cutover"
        save("cutover.private.json", cutover)
        assert time.monotonic() - frozen_at < 25, "freeze budget exceeded"
        assert config_hashes == {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in config_paths}
        stopped = True
        command(["docker", "kill", "--signal", "KILL", SERVICE], "stop-idle", timeout=30)
        command(start, "start", timeout=120)
        identity = None
        service_health = health()
        current = r.inspect(SERVICE)
        after = snapshot()
        before_db, after_db = cutover["databases"][0], after["databases"][0]
        assert all(after_db["task_hashes"].get(key) == value for key, value in before_db["task_hashes"].items()), "historical task modified"
        assert before_db["settings"] == after_db["settings"], "settings modified"
        assert all(after_db["account_config"].get(key) == value for key, value in before_db["account_config"].items()), "account configuration modified"
        assert all(after_db["manual_cost_config"].get(key) == value for key, value in before_db["manual_cost_config"].items()), "manual cost settings modified"
        assert sorted(old["Config"]["Env"]) == sorted(current["Config"]["Env"]), "environment changed"
        assert old["Mounts"] == current["Mounts"] and old["HostConfig"]["PortBindings"] == current["HostConfig"]["PortBindings"], "mounts/ports changed"
        assert set(old["NetworkSettings"]["Networks"]) == set(current["NetworkSettings"]["Networks"]), "networks changed"
        assert {key: value for key, value in M["app_hashes"].items() if key not in M["files"]} == {key: value for key, value in after["app_hashes"].items() if key not in M["files"]}, "unrelated source changed"
        assert all(after["app_hashes"].get(key) == value for key, value in M["files"].items()), "candidate source mismatch"
        assert config_hashes == {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in config_paths}
        report = {"phase": "deployed", "service": SERVICE, "image": built["image"], "files": len(M["files"]),
                  "tasks_preserved": len(before_db["task_hashes"]), "manual_cost_config_preserved": len(before_db["manual_cost_config"]),
                  "settings_routes_unchanged": True, "account_configuration_unchanged": True, "environment_unchanged": True, "other_sources_unchanged": True,
                  "health_status": service_health["status"], "paid_requests": 0}
        save("deployment.json", report)
        save("status.json", report)
    except BaseException:
        resume(identity)
        if stopped:
            # No task is replayed or retried by rollback. Leave any newly active
            # candidate running for review instead of interrupting a generation.
            current = r.inspect(SERVICE)
            if current["State"]["Running"] and current["Id"] == old["Id"] and current["Image"] == M["image_id"]:
                health("/health", mode="none")
            elif not current["State"]["Running"] or not busy(snapshot()):
                command(restore, "rollback", timeout=120)
                health("/health", mode="none")
                assert r.inspect(SERVICE)["Image"] == M["image_id"], "rollback image mismatch"
        raise
    finally:
        resume(identity)


def main():
    import argparse
    import shutil
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--deploy-path", required=True)
    args = parser.parse_args()
    assert all(c in "0123456789abcdef" for c in args.revision) and len(args.revision) == 40
    assert args.image.endswith(":" + args.revision)
    old = r.inspect(SERVICE)
    assert old["State"]["Running"], "this guarded updater requires an existing running service"
    labels = old["Config"]["Labels"]
    directory = labels["com.docker.compose.project.working_dir"]
    assert Path(directory).resolve() == Path(args.deploy_path).resolve(), "unexpected compose directory"
    configs = labels["com.docker.compose.project.config_files"].split(",")
    assert configs and all(Path(name).is_file() for name in configs)
    global ROOT, M
    ROOT = Path(directory) / "releases" / ("s3-results-" + args.revision[:16] + "-" + str(time.time_ns()))
    ROOT.mkdir(parents=True, mode=0o700)
    initial = snapshot()
    checkout = Path(__file__).resolve().parents[1]
    expected_baseline = json.loads((checkout / "docs/deploy-source-baseline.json").read_text())
    assert initial["app_hashes"] == expected_baseline, "runtime differs from the reviewed deployment baseline"
    candidate = {str(p.relative_to(checkout)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (checkout / "app").rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    # The pinned IJM base contains several inherited modules outside this repo.
    # Keep every such module byte-for-byte; the isolated image probe below must
    # match this union, so no inherited source can silently change or disappear.
    inherited = {name: value for name, value in initial["app_hashes"].items() if name not in candidate}
    expected_sources = {**inherited, **candidate}
    files = {name: value for name, value in candidate.items() if initial["app_hashes"].get(name) != value}
    for name in files:
        target = ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / name, target)
    M = {"service": SERVICE, "release": "s3-results-" + args.revision[:16], "image_id": old["Image"],
         "app_hashes": initial["app_hashes"], "files": files,
         "project": labels["com.docker.compose.project"], "directory": directory, "configs": configs}
    save("manifest.json", M)
    candidate_probe = "import pathlib,hashlib,json; b=pathlib.Path('/app'); print(json.dumps({str(p.relative_to(b)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (b/'app').rglob('*') if p.is_file() and '__pycache__' not in p.parts}))"
    actual = json.loads(command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                                 args.image, "-B", "-c", candidate_probe], "candidate-source", timeout=60))
    assert actual == expected_sources, "published image differs from reviewed checkout or inherited runtime source"
    base = "too-s3-rollback:" + old["Image"].split(":")[-1][:16]
    command(["docker", "tag", old["Image"], base], "pin-base")
    save("build.json", {"base_image": base, "image": args.image,
                        "image_id": command(["docker", "image", "inspect", "--format", "{{.Id}}", args.image], "candidate-image").strip()})
    deadline = time.monotonic() + 1200
    while busy(sources_guard()):
        if time.monotonic() >= deadline:
            save("status.json", {"phase": "deferred", "service": SERVICE, "reason": "active work"})
            raise RuntimeError("active tasks or account login prevented safe deployment")
        time.sleep(5)
    deploy()
    status = json.loads((ROOT / "status.json").read_text())
    assert status["phase"] == "deployed", "cutover deferred; the old service remains running"
    print(json.dumps({"phase": "deployed", "revision": args.revision, "receipt": str(ROOT / "deployment.json"),
                      "tasks_preserved": status["tasks_preserved"], "paid_requests": 0}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        if ROOT is not None:
            (ROOT / "failure.private.log").write_text(traceback.format_exc())
        raise
