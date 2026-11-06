#!/usr/bin/env python3
"""A fake `systemctl` for board-script end-to-end tests.

Implements just enough of the interface rk-deploy uses: it actually
starts/stops the unit's ExecStart process so health checks exercise
real behavior. State lives in $FAKE_SYSTEMCTL_DIR; unit files are
read from $RKDEPLOY_SYSTEMD_DIR. Test-only: never shipped in packages.
"""
import os
import signal
import subprocess
import sys

STATE = os.environ["FAKE_SYSTEMCTL_DIR"]
UNITS = os.environ["RKDEPLOY_SYSTEMD_DIR"]
os.makedirs(STATE, exist_ok=True)


def pidfile(unit):
    return os.path.join(STATE, unit + ".pid")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(unit):
    try:
        return int(open(pidfile(unit)).read().strip())
    except Exception:
        return 0


def parse_unit(unit):
    exec_start, workdir, env = None, None, {}
    path = os.path.join(UNITS, unit)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("ExecStart="):
                exec_start = line[len("ExecStart="):]
            elif line.startswith("WorkingDirectory="):
                workdir = line[len("WorkingDirectory="):]
            elif line.startswith("Environment="):
                kv = line[len("Environment="):]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    env[k] = v
    return exec_start, workdir, env


def stop(unit):
    pid = read_pid(unit)
    if pid and alive(pid):
        os.kill(pid, signal.SIGTERM)
    try:
        os.remove(pidfile(unit))
    except OSError:
        pass


def main(argv):
    args = [a for a in argv if a != "--value"]
    if not args:
        return 0
    cmd = args[0]
    unit = args[-1] if len(args) > 1 else ""
    if cmd == "--version":
        print("systemd 249 (fake)")
        return 0
    if cmd == "daemon-reload":
        return 0
    if cmd == "enable":
        open(os.path.join(STATE, unit + ".enabled"), "w").close()
        return 0
    if cmd == "disable":
        try:
            os.remove(os.path.join(STATE, unit + ".enabled"))
        except OSError:
            pass
        return 0
    if cmd == "is-enabled":
        if os.path.exists(os.path.join(STATE, unit + ".enabled")):
            print("enabled")
            return 0
        if not os.path.isfile(os.path.join(UNITS, unit)):
            # Match systemd 249: missing units have empty stdout (error on stderr).
            return 4
        print("disabled")
        return 1
    if cmd == "restart" or cmd == "start":
        stop(unit)
        exec_start, workdir, env = parse_unit(unit)
        full_env = dict(os.environ)
        full_env.update(env)
        log = open(os.path.join(STATE, unit + ".log"), "ab")
        proc = subprocess.Popen(exec_start.split(), cwd=workdir or "/",
                                env=full_env, stdout=log, stderr=log,
                                start_new_session=True)
        open(pidfile(unit), "w").write(str(proc.pid))
        return 0
    if cmd == "stop":
        stop(unit)
        return 0
    if cmd == "is-active":
        pid = read_pid(unit)
        if pid and alive(pid):
            print("active")
            return 0
        print("inactive")
        return 3
    if cmd == "show":
        prop = args[1] if len(args) > 1 else ""
        if prop == "MainPID":
            print(f"MainPID={read_pid(unit)}")
        elif prop == "ExecMainStartTimestamp":
            print("ExecMainStartTimestamp=Mon 2026-11-02 00:00:00 JST")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
