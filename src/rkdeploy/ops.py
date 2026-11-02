"""Controller-side operations: deploy / rollback / status / releases."""
import json
import os
import re
import tarfile
import tempfile
from importlib import resources

from . import fleet as fl

RESULT_RE = re.compile(r"RKDEPLOY_RESULT=(\S+)((?:\s+\S+)*)")


def board_script(name):
    return resources.files("rkdeploy.board").joinpath(name).read_text(encoding="utf-8")


def parse_result(output):
    """Extract the RKDEPLOY_RESULT line a board script prints."""
    found = None
    for line in (output or "").splitlines():
        m = RESULT_RE.search(line)
        if m:
            kv = dict(p.split("=", 1) for p in m.group(2).split() if "=" in p)
            found = (m.group(1), kv)
    return found


def read_package_meta(package_path):
    with tarfile.open(package_path, "r:gz") as tf:
        pkg = json.loads(tf.extractfile("package.json").read().decode())
    return pkg


def deploy_host(host, package_path, app, no_auto_rollback=False):
    root = host.root_for(app)
    inbox = f"/tmp/rkdeploy-inbox-{app}"
    remote_pkg = f"{inbox}/{os.path.basename(package_path)}"
    r = fl.run_ssh(host, f"mkdir -p {inbox}")
    if r.returncode != 0:
        return {"alias": host.alias, "outcome": "unreachable", "detail": r.stderr.strip()[:300]}
    r = fl.scp_to(host, package_path, remote_pkg)
    if r.returncode != 0:
        return {"alias": host.alias, "outcome": "upload-failed", "detail": r.stderr.strip()[:300]}
    env = "RKDEPLOY_NO_AUTO_ROLLBACK=1 " if no_auto_rollback else ""
    cmd = (f"d=$(mktemp -d) && tar -xzf {remote_pkg} -C $d install.sh && "
           f"{env}sh $d/install.sh {remote_pkg} {root}; rc=$?; rm -rf $d; exit $rc")
    r = fl.run_ssh(host, cmd, timeout=900)
    parsed = parse_result(r.stdout)
    outcome = parsed[0] if parsed else ("error" if r.returncode else "unknown")
    return {"alias": host.alias, "outcome": outcome,
            "version": (parsed[1].get("version") if parsed else None),
            "rc": r.returncode, "log": (r.stdout + r.stderr).strip()[-1200:]}


def rollback_host(host, app, to=None):
    root = host.root_for(app)
    script = board_script("rollback.sh")
    cmd = f"sh -s {root} {app}" + (f" {to}" if to else "")
    r = fl.run_ssh(host, cmd, stdin_data=script, timeout=600)
    parsed = parse_result(r.stdout)
    outcome = parsed[0] if parsed else ("error" if r.returncode else "unknown")
    return {"alias": host.alias, "outcome": outcome,
            "version": (parsed[1].get("version") if parsed else None),
            "rc": r.returncode, "log": (r.stdout + r.stderr).strip()[-1200:]}


def probe_host(host, app):
    root = host.root_for(app)
    script = board_script("probe.sh")
    r = fl.run_ssh(host, f"sh -s {root} {app}", stdin_data=script, timeout=120)
    if r.returncode != 0 and "PROBE=1" not in r.stdout:
        return {"alias": host.alias, "reachable": False}
    kv = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return parse_probe(host.alias, kv)


def parse_probe(alias, kv):
    def as_int(v, default=None):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    rec = {
        "alias": alias,
        "reachable": kv.get("PROBE") == "1",
        "installed": kv.get("INSTALLED") == "1",
        "version": kv.get("VERSION") or None,
        "previous": kv.get("PREVIOUS") or None,
        "service": kv.get("SERVICE_ACTIVE"),
        "enabled": kv.get("SERVICE_ENABLED"),
        "main_pid": as_int(kv.get("MAIN_PID"), 0),
        "service_since": kv.get("SERVICE_SINCE") or None,
        "releases": [x for x in (kv.get("RELEASES") or "").split(",") if x],
        "disk_free_pct": as_int(kv.get("DISK_FREE_PCT")),
        "temps_c": {"soc": as_int(kv.get("TEMP_SOC_C")), "max": as_int(kv.get("TEMP_MAX_C"))},
        "health": kv.get("HEALTH"),
        "journal_errors_1h": as_int(kv.get("JOURNAL_ERRORS_1H"), -1),
    }
    rec["verdict"] = verdict_for(rec)
    return rec


def verdict_for(rec):
    if not rec.get("reachable"):
        return "UNREACHABLE"
    if not rec.get("installed"):
        return "NOT_INSTALLED"
    if rec.get("service") != "active" or rec.get("health") == "fail":
        return "UNHEALTHY"
    warn = False
    if rec.get("disk_free_pct") is not None:
        if rec["disk_free_pct"] < 5:
            return "UNHEALTHY"
        if rec["disk_free_pct"] < 10:
            warn = True
    tmax = (rec.get("temps_c") or {}).get("max")
    if tmax is not None:
        if tmax >= 85:
            return "UNHEALTHY"
        if tmax >= 75:
            warn = True
    if len(rec.get("releases") or []) < 2:
        warn = True
    return "ATTENTION" if warn else "HEALTHY"


def build_status_report(records):
    """Add fleet-level drift detection and the summary block."""
    versions = [r["version"] for r in records if r.get("reachable") and r.get("version")]
    majority = None
    if versions:
        majority = max(set(versions), key=versions.count)
    for r in records:
        r["version_drift"] = bool(majority and r.get("version") and r["version"] != majority)
        if r["version_drift"] and r.get("verdict") == "HEALTHY":
            r["verdict"] = "ATTENTION"
    order = {"UNHEALTHY": 0, "UNREACHABLE": 0, "ATTENTION": 1, "NOT_INSTALLED": 1, "HEALTHY": 2}
    worst = None
    for r in records:
        v = r.get("verdict", "HEALTHY")
        if worst is None or order.get(v, 2) < order.get(worst, 2):
            worst = v
    from . import __version__
    import datetime
    return {
        "schema": 1,
        "tool_version": __version__,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "fleet_verdict": worst or "HEALTHY",
        "fleet": records,
    }
