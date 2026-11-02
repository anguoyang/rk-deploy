"""rk-deploy test suite (unittest style; also runs under pytest)."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from rkdeploy import manifest as mf  # noqa: E402
from rkdeploy.fleet import Fleet, load_fleet  # noqa: E402
from rkdeploy.ops import build_status_report, parse_probe, parse_result  # noqa: E402
from rkdeploy.pack import build_package  # noqa: E402
from rkdeploy.unit import render_unit_template  # noqa: E402

DEMO = os.path.join(REPO, "examples", "demo")
FAKE = os.path.join(REPO, "tests", "fake_systemctl.py")


_counter = [0]


def make_app(tmp, version="1.0.0", broken_health=False):
    _counter[0] += 1
    app = os.path.join(tmp, f"app-{version}-{_counter[0]}")
    shutil.copytree(DEMO, app)
    y = os.path.join(app, "rkdeploy.yaml")
    text = open(y).read().replace("version: 1.0.0", f"version: {version}")
    if broken_health:
        text = text.replace("shared/heartbeat.json", "shared/never-written.json")
        text = text.replace("timeout_sec: 15", "timeout_sec: 3")
    open(y, "w").write(text)
    return app


class ManifestTests(unittest.TestCase):
    def test_demo_manifest_loads(self):
        m = mf.load_manifest(os.path.join(DEMO, "rkdeploy.yaml"))
        self.assertEqual(m["name"], "demo")
        self.assertEqual(m["runtime"]["entry"], "app.py")
        self.assertEqual(m["healthcheck"]["type"], "file")

    def _bad(self, mutate):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            app = make_app(td)
            y = os.path.join(app, "rkdeploy.yaml")
            text = open(y).read()
            open(y, "w").write(mutate(text))
            with self.assertRaises(mf.ManifestError):
                mf.load_manifest(y)

    def test_bad_name_rejected(self):
        self._bad(lambda t: t.replace("name: demo", "name: Demo_X"))

    def test_missing_healthcheck_rejected(self):
        self._bad(lambda t: t.split("healthcheck:")[0])

    def test_keep_releases_floor(self):
        self._bad(lambda t: t.replace("keep_releases: 3", "keep_releases: 1"))

    def test_entry_traversal_rejected(self):
        self._bad(lambda t: t.replace("entry: app.py", "entry: ../evil.py"))


class PackTests(unittest.TestCase):
    def test_package_contents_and_reproducibility(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p1, _ = build_package(os.path.join(DEMO, "rkdeploy.yaml"), os.path.join(td, "a"))
            p2, _ = build_package(os.path.join(DEMO, "rkdeploy.yaml"), os.path.join(td, "b"))
            h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
            self.assertEqual(h(p1), h(p2), "same source must build identical bytes")
            with tarfile.open(p1) as tf:
                names = sorted(tf.getnames())
                pkg = json.loads(tf.extractfile("package.json").read())
                sums = tf.extractfile("SHA256SUMS").read().decode()
            for expected in ("app.py", "models/weights.bin", "package.json",
                             "package.env", "service.template", "install.sh",
                             "DEPENDENCIES.json", "rkdeploy.yaml", "SHA256SUMS"):
                self.assertIn(expected, names)
            self.assertEqual(pkg["name"], "demo")
            self.assertEqual({f["path"] for f in pkg["files"]},
                             {"app.py", "models/weights.bin"})
            self.assertIn("app.py", sums)

    def test_undeclared_files_not_packed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            app = make_app(td)
            open(os.path.join(app, "secret-notes.txt"), "w").write("should not travel")
            p, _ = build_package(os.path.join(app, "rkdeploy.yaml"), os.path.join(td, "out"))
            with tarfile.open(p) as tf:
                self.assertNotIn("secret-notes.txt", tf.getnames())


class UnitTests(unittest.TestCase):
    def test_template_placeholders_and_env(self):
        m = mf.load_manifest(os.path.join(DEMO, "rkdeploy.yaml"))
        t = render_unit_template(m)
        self.assertIn("ExecStart=/usr/bin/python3 @INSTALL_ROOT@/current/app.py", t)
        self.assertIn("User=@RUN_USER@", t)
        self.assertIn("Environment=RKDEPLOY_VERSION=@APP_VERSION@", t)


class FleetTests(unittest.TestCase):
    def test_load_and_select(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "fleet.yaml")
            open(f, "w").write(
                "hosts:\n"
                "  - {alias: a, address: 192.0.2.1, user: root, tags: [pilot]}\n"
                "  - {alias: b, address: 192.0.2.2, user: ops, install_root: /srv/x}\n")
            fleet = load_fleet(f)
            self.assertEqual([h.alias for h in fleet.select("tag:pilot")], ["a"])
            self.assertEqual(fleet.select("b")[0].root_for("demo"), "/srv/x")
            self.assertEqual(fleet.select("a")[0].root_for("demo"), "/opt/demo")
            with self.assertRaises(Exception):
                fleet.select("nope")


class ProbeTests(unittest.TestCase):
    def test_parse_and_verdicts(self):
        kv = {"PROBE": "1", "INSTALLED": "1", "VERSION": "1.0.0", "SERVICE_ACTIVE": "active",
              "SERVICE_ENABLED": "enabled", "MAIN_PID": "42", "RELEASES": "1.0.0,1.1.0",
              "DISK_FREE_PCT": "50", "TEMP_SOC_C": "49", "TEMP_MAX_C": "51",
              "HEALTH": "ok", "JOURNAL_ERRORS_1H": "0"}
        rec = parse_probe("b1", kv)
        self.assertEqual(rec["verdict"], "HEALTHY")
        kv2 = dict(kv, DISK_FREE_PCT="3")
        self.assertEqual(parse_probe("b1", kv2)["verdict"], "UNHEALTHY")
        kv3 = dict(kv, TEMP_MAX_C="80")
        self.assertEqual(parse_probe("b1", kv3)["verdict"], "ATTENTION")

    def test_drift_detection(self):
        base = {"alias": "x", "reachable": True, "installed": True, "version": "1.0.0",
                "service": "active", "health": "ok", "disk_free_pct": 50,
                "temps_c": {"soc": 40, "max": 41}, "releases": ["1.0.0", "0.9.0"],
                "verdict": "HEALTHY"}
        r1 = dict(base, alias="a")
        r2 = dict(base, alias="b", version="1.1.0")
        r3 = dict(base, alias="c", version="1.1.0")
        rep = build_status_report([r1, r2, r3])
        self.assertTrue(rep["fleet"][0]["version_drift"])
        self.assertEqual(rep["fleet"][0]["verdict"], "ATTENTION")

    def test_parse_result_line(self):
        out = "noise\nRKDEPLOY_RESULT=rolled-back version=1.0.0 failed-version=1.1.0\n"
        self.assertEqual(parse_result(out)[0], "rolled-back")
        self.assertEqual(parse_result(out)[1]["failed-version"], "1.1.0")
        self.assertIsNone(parse_result("nothing here"))


class BoardEndToEndTests(unittest.TestCase):
    """Run the real board scripts against a fake systemctl that really
    starts the demo app: install v1 -> install v2 -> rollback -> v1,
    plus the auto-rollback path for a broken release."""

    def setUp(self):
        import tempfile
        self.td = tempfile.mkdtemp(prefix="rkdeploy-e2e-")
        self.root = os.path.join(self.td, "board", "opt", "demo")
        self.sysd = os.path.join(self.td, "systemd")
        self.state = os.path.join(self.td, "fake-systemctl")
        os.makedirs(self.sysd)
        os.makedirs(self.state)
        self.fake = os.path.join(self.td, "systemctl")
        shutil.copy(FAKE, self.fake)
        os.chmod(self.fake, 0o755)
        self.env = dict(os.environ,
                        RKDEPLOY_SYSTEMCTL=self.fake,
                        RKDEPLOY_SYSTEMD_DIR=self.sysd,
                        FAKE_SYSTEMCTL_DIR=self.state)

    def tearDown(self):
        subprocess.run([self.fake, "stop", "rkdeploy-demo.service"],
                       env=self.env, capture_output=True)
        shutil.rmtree(self.td, ignore_errors=True)

    def _installer_from(self, pkg):
        out = os.path.join(self.td, f"install-{os.path.basename(pkg)}.sh")
        with tarfile.open(pkg) as tf:
            data = tf.extractfile("install.sh").read()
        open(out, "wb").write(data)
        return out

    def _board_script(self, name):
        from rkdeploy.ops import board_script
        p = os.path.join(self.td, name)
        open(p, "w").write(board_script(name))
        return p

    def _current(self):
        return os.path.basename(os.readlink(os.path.join(self.root, "current")))

    def _heartbeat_version(self, expected=None):
        hb = os.path.join(self.root, "shared", "heartbeat.json")
        deadline = time.time() + 10
        seen = None
        while time.time() < deadline:
            if os.path.exists(hb):
                try:
                    seen = json.load(open(hb))["version"]
                except Exception:
                    seen = None
                if expected is None or seen == expected:
                    return seen
            time.sleep(0.3)
        return seen

    def _install2(self, version, broken=False):
        app = make_app(self.td, version, broken_health=broken)
        pkg, _ = build_package(os.path.join(app, "rkdeploy.yaml"),
                               os.path.join(self.td, f"dist-{version}"))
        script = self._installer_from(pkg)
        return subprocess.run(["sh", script, pkg, self.root],
                              env=self.env, capture_output=True, text=True, timeout=120)


# The lifecycle test proper lives here (kept separate so the fake's
# process management is exercised exactly as on a real board).
class LifecycleTests(BoardEndToEndTests):
    def test_upgrade_rollback_and_auto_rollback(self):
        r = self._install2("1.0.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._current(), "1.0.0")
        self.assertEqual(self._heartbeat_version("1.0.0"), "1.0.0")
        state = json.load(open(os.path.join(self.root, "state.json")))
        self.assertEqual(state["current"], "1.0.0")
        marker = os.path.join(self.root, "shared", "marker.txt")
        open(marker, "w").write("keep-me")

        r = self._install2("1.0.0")  # same version again: immutable, refuse
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)

        r = self._install2("1.1.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._current(), "1.1.0")
        self.assertEqual(self._heartbeat_version("1.1.0"), "1.1.0")
        self.assertEqual(open(marker).read(), "keep-me")

        rb = subprocess.run(["sh", self._board_script("rollback.sh"),
                             self.root, "demo"],
                            env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(rb.returncode, 0, rb.stdout + rb.stderr)
        self.assertEqual(self._current(), "1.0.0")
        self.assertEqual(self._heartbeat_version("1.0.0"), "1.0.0")

        r = self._install2("1.2.0", broken=True)  # health never passes
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertEqual(self._current(), "1.0.0")
        self.assertEqual(self._heartbeat_version("1.0.0"), "1.0.0")
        state = json.load(open(os.path.join(self.root, "state.json")))
        self.assertEqual(state["current"], "1.0.0")
        self.assertTrue(any(h["result"] == "rolled-back" for h in state["history"]))

    def test_probe_reports(self):
        r = self._install2("1.0.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = subprocess.run(["sh", self._board_script("probe.sh"), self.root, "demo"],
                             env=self.env, capture_output=True, text=True, timeout=60)
        kv = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
        self.assertEqual(kv["VERSION"], "1.0.0")
        self.assertEqual(kv["SERVICE_ACTIVE"], "active")
        self.assertEqual(kv["HEALTH"], "ok")
        rec = parse_probe("testboard", kv)
        self.assertIn(rec["verdict"], ("HEALTHY", "ATTENTION"))


if __name__ == "__main__":
    unittest.main()
