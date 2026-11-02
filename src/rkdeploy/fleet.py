"""Fleet definition and SSH transport.

Transport is the stock OpenSSH client: the user's ~/.ssh/config,
keys and agent apply exactly as if they ran ssh/scp themselves.
rk-deploy never asks for or stores credentials. Each board operation
uses the minimum number of connections (one upload + one execution
for deploy; a single execution for status/rollback/releases).
"""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor


class FleetError(ValueError):
    pass


class Host:
    def __init__(self, raw):
        if not isinstance(raw, dict) or not raw.get("alias") or not raw.get("address"):
            raise FleetError("each host needs at least alias and address")
        self.alias = str(raw["alias"])
        self.address = str(raw["address"])
        self.user = str(raw.get("user", "root"))
        self.install_root = raw.get("install_root")  # None -> /opt/<app>
        self.tags = [str(t) for t in (raw.get("tags") or [])]
        opts = raw.get("ssh_options") or []
        if not isinstance(opts, list) or not all(isinstance(o, str) for o in opts):
            raise FleetError(f"host {self.alias}: ssh_options must be a list of strings")
        self.ssh_options = opts
        self.identity_file = raw.get("identity_file")

    def root_for(self, app):
        return self.install_root or f"/opt/{app}"

    def ssh_base(self, connect_timeout):
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}"]
        for o in self.ssh_options:
            cmd += ["-o", o]
        if self.identity_file:
            cmd += ["-i", os.path.expanduser(str(self.identity_file))]
        return cmd

    def target(self):
        return f"{self.user}@{self.address}"


class Fleet:
    def __init__(self, hosts, connect_timeout=15):
        self.hosts = hosts
        self.connect_timeout = connect_timeout

    def select(self, selector):
        """selector: None (all), comma list of aliases, or tag:<tag>."""
        if not selector:
            return list(self.hosts)
        out = []
        for part in selector.split(","):
            part = part.strip()
            if part.startswith("tag:"):
                out.extend(h for h in self.hosts if part[4:] in h.tags and h not in out)
            else:
                match = [h for h in self.hosts if h.alias == part]
                if not match:
                    raise FleetError(f"unknown host alias: {part}")
                out.extend(match)
        return out


def load_fleet(path):
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise FleetError("PyYAML is required on the controller") from e
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise FleetError(f"fleet file not found: {path}")
    if not isinstance(raw, dict) or not raw.get("hosts"):
        raise FleetError("fleet file must define hosts:")
    ssh = raw.get("ssh") or {}
    timeout = int(ssh.get("connect_timeout_sec", 15))
    return attach_timeouts(Fleet([Host(h) for h in raw["hosts"]], connect_timeout=timeout))


def run_ssh(host, remote_cmd, stdin_data=None, timeout=300):
    cmd = host.ssh_base(host_timeout(host)) + [host.target(), remote_cmd]
    r = subprocess.run(cmd, input=stdin_data, capture_output=True, text=True, timeout=timeout)
    return r


def host_timeout(host):
    # Fleet stores the timeout; Host keeps a back-reference set at load.
    return getattr(host, "_timeout", 15)


def scp_to(host, local_path, remote_path, timeout=600):
    cmd = ["scp", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={host_timeout(host)}"]
    for o in host.ssh_options:
        cmd += ["-o", o]
    if host.identity_file:
        cmd += ["-i", os.path.expanduser(str(host.identity_file))]
    cmd += [local_path, f"{host.target()}:{remote_path}"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def attach_timeouts(fleet):
    for h in fleet.hosts:
        h._timeout = fleet.connect_timeout
    return fleet


def run_fleet(hosts, fn, jobs=4):
    """Run fn(host) for each host; returns {alias: result-or-exception}."""
    results = {}
    if jobs and jobs > 1 and len(hosts) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(fn, h): h for h in hosts}
            for fut, h in futs.items():
                try:
                    results[h.alias] = fut.result()
                except Exception as e:  # per-host failure must not sink the fleet
                    results[h.alias] = e
    else:
        for h in hosts:
            try:
                results[h.alias] = fn(h)
            except Exception as e:
                results[h.alias] = e
    return results
