"""Package building: manifest + declared payload -> <name>-<version>.tar.gz.

Only files the manifest declares are packed. The archive is built
deterministically (sorted entries, normalized timestamps/owners) so
the same source tree always produces the same package bytes.
"""
import gzip
import hashlib
import io
import os
import tarfile
from importlib import resources

from . import manifest as mf
from .unit import render_unit_template

GENERATED = ("rkdeploy.yaml", "package.json", "package.env", "SHA256SUMS",
             "service.template", "install.sh", "DEPENDENCIES.json")


def _board_script(name):
    ref = resources.files("rkdeploy.board").joinpath(name)
    return ref.read_bytes()


def _expand_payload(app_dir, rel):
    """Expand one declared path (file or directory) to (arcname, abspath)."""
    base = os.path.join(app_dir, rel)
    if not os.path.exists(base):
        raise mf.ManifestError(f"declared payload path does not exist: {rel}")
    if os.path.isfile(base):
        return [(rel, base)]
    out = []
    for root, dirs, files in os.walk(base):
        dirs.sort()
        for fn in sorted(files):
            full = os.path.join(root, fn)
            out.append((os.path.relpath(full, app_dir), full))
    out.sort()
    return out


def build_package(manifest_path, out_dir):
    app_dir = os.path.dirname(os.path.abspath(manifest_path))
    m = mf.load_manifest(manifest_path)

    expanded = []
    for rel in mf.payload_paths(m):
        expanded.extend(_expand_payload(app_dir, rel))
    seen = set()
    payload = []
    for arc, full in expanded:
        if arc not in seen:
            seen.add(arc)
            payload.append((arc, full))
    payload.sort(key=lambda x: x[0])

    hashes = {}
    blobs = {}
    for arc, full in payload:
        with open(full, "rb") as f:
            data = f.read()
        blobs[arc] = data
        hashes[arc] = hashlib.sha256(data).hexdigest()

    with open(manifest_path, "rb") as f:
        manifest_blob = f.read()
    entries = [{"path": arc, "sha256": hashes[arc]} for arc, _ in payload]
    members = {
        "rkdeploy.yaml": manifest_blob,
        "package.json": mf.render_package_json(m, entries).encode(),
        "package.env": mf.render_package_env(m).encode(),
        "DEPENDENCIES.json": mf.render_dependencies_json(m).encode(),
        "service.template": render_unit_template(m).encode(),
        "install.sh": _board_script("install.sh"),
    }
    sums = "".join(f"{hashes[arc]}  {arc}\n" for arc, _ in payload)
    members["SHA256SUMS"] = sums.encode()

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{m['name']}-{m['version']}.tar.gz")
    names = sorted(set(list(members) + [arc for arc, _ in payload]))
    with open(out_path, "wb") as raw:
        # gzip header pinned (mtime=0, no original name) + normalized member
        # metadata below: the same source tree always builds the same bytes.
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
                for name in names:
                    data = members[name] if name in members else blobs[name]
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o755 if name == "install.sh" else 0o644
                    tf.addfile(info, io.BytesIO(data))
    return out_path, m
