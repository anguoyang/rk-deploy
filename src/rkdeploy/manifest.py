"""Manifest loading, validation and rendering.

The author-facing manifest is ``rkdeploy.yaml`` (schema 1). At pack
time it is rendered into machine-readable files that travel inside
the package:

- ``package.json``  -- full rendered metadata (controller tooling)
- ``package.env``   -- flat KEY=VALUE facts the board installer needs
- ``DEPENDENCIES.json`` -- the preflight checklist from ``depends:``

The board never parses YAML; it reads only these rendered files.
"""
import json
import re

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
RUNTIME_TYPES = ("python", "binary", "shell")
RESTART_VALUES = ("no", "always", "on-failure", "on-abnormal")
HEALTH_TYPES = ("file", "command", "systemd-only")
DEP_KEYS = ("binaries", "apt", "python", "libs", "devices")


class ManifestError(ValueError):
    """Raised for any manifest problem (CLI exit code 2)."""


def _require(cond, msg):
    if not cond:
        raise ManifestError(msg)


def _str_map(value, what):
    if value is None:
        return {}
    _require(isinstance(value, dict), f"{what} must be a mapping")
    out = {}
    for k, v in value.items():
        _require(isinstance(k, str) and isinstance(v, (str, int, float)),
                 f"{what}.{k} must be a scalar")
        out[str(k)] = str(v)
    return out


def _str_list(value, what):
    if value is None:
        return []
    _require(isinstance(value, list), f"{what} must be a list")
    out = []
    for item in value:
        _require(isinstance(item, dict) and isinstance(item.get("path"), str),
                 f"{what} entries must be mappings with a path")
        p = item["path"]
        _require(not p.startswith("/") and ".." not in p.split("/"),
                 f"{what} path must be relative and stay inside the app dir: {p}")
        out.append(p)
    return out


def load_manifest(path):
    """Read and validate rkdeploy.yaml; return a normalized dict."""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - environment problem
        raise ManifestError("PyYAML is required on the controller: pip install pyyaml") from e
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise ManifestError(f"manifest not found: {path}")
    except Exception as e:
        raise ManifestError(f"cannot parse manifest {path}: {e}")
    _require(isinstance(raw, dict), "manifest must be a mapping")

    m = {}
    name = raw.get("name")
    _require(isinstance(name, str) and NAME_RE.match(name),
             "name is required and must match ^[a-z][a-z0-9-]{1,31}$")
    m["name"] = name
    version = raw.get("version")
    _require(isinstance(version, (str, int, float)) and VERSION_RE.match(str(version)),
             "version is required and must be semver (e.g. 1.1.0)")
    m["version"] = str(version)
    m["description"] = str(raw.get("description", ""))

    runtime = raw.get("runtime") or {}
    _require(isinstance(runtime, dict), "runtime must be a mapping")
    rtype = runtime.get("type", "python")
    _require(rtype in RUNTIME_TYPES, f"runtime.type must be one of {RUNTIME_TYPES}")
    entry = runtime.get("entry")
    _require(isinstance(entry, str) and entry and not entry.startswith("/")
             and ".." not in entry.split("/"),
             "runtime.entry is required and must be a relative path")
    m["runtime"] = {"type": rtype, "entry": entry}

    service = raw.get("service") or {}
    _require(isinstance(service, dict), "service must be a mapping")
    restart = service.get("restart", "on-failure")
    _require(restart in RESTART_VALUES, f"service.restart must be one of {RESTART_VALUES}")
    m["service"] = {
        "user": str(service.get("user", "root")),
        "restart": restart,
        "restart_sec": int(service.get("restart_sec", 2)),
        "env": _str_map(service.get("env"), "service.env"),
    }

    m["models"] = _str_list(raw.get("models"), "models")
    m["files"] = _str_list(raw.get("files"), "files")

    depends = raw.get("depends") or {}
    _require(isinstance(depends, dict), "depends must be a mapping")
    m["depends"] = {}
    for key in DEP_KEYS:
        val = depends.get(key) or []
        _require(isinstance(val, list) and all(isinstance(x, str) for x in val),
                 f"depends.{key} must be a list of strings")
        m["depends"][key] = list(val)

    health = raw.get("healthcheck")
    _require(isinstance(health, dict), "healthcheck is required")
    htype = health.get("type")
    _require(htype in HEALTH_TYPES, f"healthcheck.type must be one of {HEALTH_TYPES}")
    h = {"type": htype, "timeout_sec": int(health.get("timeout_sec", 15))}
    if htype == "file":
        _require(isinstance(health.get("path"), str) and health["path"],
                 "healthcheck.path is required for type: file")
        h["path"] = health["path"]
    elif htype == "command":
        _require(isinstance(health.get("command"), str) and health["command"],
                 "healthcheck.command is required for type: command")
        h["command"] = health["command"]
    m["healthcheck"] = h

    keep = raw.get("keep_releases", 3)
    _require(isinstance(keep, int) and keep >= 2,
             "keep_releases must be an integer >= 2 (current + a rollback target)")
    m["keep_releases"] = keep
    m["schema"] = 1
    return m


def payload_paths(m):
    """Every payload path the package must contain, in stable order."""
    paths = [m["runtime"]["entry"]]
    for p in m["files"] + m["models"]:
        if p not in paths:
            paths.append(p)
    return paths


def render_package_json(m, file_entries):
    """file_entries: list of {"path": arcname, "sha256": hex} for every
    payload file actually packed (declared directories pre-expanded)."""
    doc = {
        "format": 1,
        "name": m["name"],
        "version": m["version"],
        "description": m["description"],
        "runtime": m["runtime"],
        "service": m["service"],
        "healthcheck": m["healthcheck"],
        "keep_releases": m["keep_releases"],
        "depends": m["depends"],
        "files": file_entries,
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def render_package_env(m):
    """Flat facts for the board installer (no JSON parsing in shell).

    Values use @INSTALL_ROOT@ placeholders where board-level facts are
    needed; the installer resolves them into install.env at install
    time.
    """
    import shlex
    h = m["healthcheck"]
    d = m["depends"]
    pairs = [
        ("APP_NAME", m["name"]),
        ("APP_VERSION", m["version"]),
        ("SERVICE_USER", m["service"]["user"]),
        ("KEEP_RELEASES", str(m["keep_releases"])),
        ("HEALTH_TYPE", h["type"]),
        ("HEALTH_TIMEOUT", str(h["timeout_sec"])),
        ("HEALTH_PATH", h.get("path", "")),
        ("HEALTH_COMMAND", h.get("command", "")),
        ("DEPS_BINARIES", " ".join(d["binaries"])),
        ("DEPS_LIBS", " ".join(d["libs"])),
        ("DEPS_APT", " ".join(d["apt"])),
        ("DEPS_PYTHON", " ".join(d["python"])),
        ("DEPS_DEVICES", " ".join(d["devices"])),
    ]
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in pairs)


def render_dependencies_json(m):
    return json.dumps(m["depends"], indent=2, sort_keys=True) + "\n"
