"""rkdeploy command line."""
import argparse
import json
import sys

from . import __version__, ops
from .fleet import FleetError, load_fleet, run_fleet
from .manifest import ManifestError
from .pack import build_package

# exit codes (docs/SPEC.md)
OK, USAGE, VERIFY, PARTIAL, ROLLED_BACK, FAILED = 0, 2, 4, 5, 6, 7


def _load(args):
    return load_fleet(args.fleet)


def _select(fleet, args):
    return fleet.select(getattr(args, "hosts", None))


def cmd_pack(args):
    out, m = build_package(args.manifest, args.out)
    print(f"packed {m['name']} {m['version']} -> {out}")
    return OK


def cmd_deploy(args):
    meta = ops.read_package_meta(args.package)
    app = meta["name"]
    fleet = _load(args)
    hosts = _select(fleet, args)
    if args.dry_run:
        unit_template = ops.read_package_unit_template(args.package)
        for host in hosts:
            preview = ops.preview_deploy_host(host, args.package, meta, unit_template)
            print(f"{preview['alias']}: dry-run")
            print(f"  Package: {preview['package']}")
            print(f"  Target root: {preview['target_root']}")
            print(f"  Unit file: {preview['unit_name']}")
            print("  Rendered unit:")
            unit_content = preview["unit_content"]
            print(unit_content, end="" if unit_content.endswith("\n") else "\n")
        print("Dry run complete; no SSH commands, uploads, or board-side actions were performed.")
        return OK

    results = run_fleet(hosts, lambda h: ops.deploy_host(h, args.package, app, args.no_auto_rollback),
                        jobs=args.jobs)
    worst = OK
    for h in hosts:
        r = results[h.alias]
        if isinstance(r, Exception):
            r = {"alias": h.alias, "outcome": "error", "log": str(r)}
        print(f"{h.alias}: {r['outcome']}" + (f" version={r.get('version')}" if r.get("version") else ""))
        if r["outcome"] == "ok":
            continue
        if r["outcome"] == "rolled-back":
            worst = max(worst, ROLLED_BACK)
        elif r["outcome"] == "aborted":
            worst = max(worst, VERIFY)
        elif r["outcome"] in ("unreachable", "upload-failed", "error", "unknown"):
            worst = max(worst, PARTIAL)
        else:  # failed / rollback-failed: a board left in a bad state
            worst = FAILED
    return worst


def cmd_rollback(args):
    fleet = _load(args)
    hosts = _select(fleet, args)
    results = run_fleet(hosts, lambda h: ops.rollback_host(h, args.app, args.to), jobs=args.jobs)
    worst = OK
    for h in hosts:
        r = results[h.alias]
        if isinstance(r, Exception):
            r = {"alias": h.alias, "outcome": "error", "log": str(r)}
        print(f"{h.alias}: {r['outcome']}" + (f" version={r.get('version')}" if r.get("version") else ""))
        if r["outcome"] == "ok":
            continue
        worst = FAILED if r["outcome"] != "aborted" else VERIFY
        if len(hosts) > 1 and r["outcome"] in ("error",):
            worst = PARTIAL
    return worst


def cmd_status(args):
    fleet = _load(args)
    hosts = _select(fleet, args)
    results = run_fleet(hosts, lambda h: ops.probe_host(h, args.app), jobs=args.jobs)
    records = []
    for h in hosts:
        r = results[h.alias]
        if isinstance(r, Exception):
            r = {"alias": h.alias, "reachable": False, "verdict": "UNREACHABLE"}
        records.append(r)
    report = ops.build_status_report(records)
    for r in records:
        temps = (r.get("temps_c") or {})
        print(f"{r['alias']}: {r.get('verdict')} version={r.get('version') or '-'} "
              f"service={r.get('service') or '-'} disk_free={r.get('disk_free_pct')}% "
              f"temp_max={temps.get('max')}C" + (" DRIFT" if r.get("version_drift") else ""))
    print(f"fleet verdict: {report['fleet_verdict']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"report written to {args.json}")
    return OK if report["fleet_verdict"] in ("HEALTHY", "ATTENTION") else PARTIAL


def cmd_releases(args):
    fleet = _load(args)
    hosts = _select(fleet, args)
    results = run_fleet(hosts, lambda h: ops.probe_host(h, args.app), jobs=args.jobs)
    for h in hosts:
        r = results[h.alias]
        if isinstance(r, Exception) or not r.get("reachable"):
            print(f"{h.alias}: UNREACHABLE")
            continue
        rel = ", ".join(r.get("releases") or []) or "-"
        print(f"{h.alias}: current={r.get('version') or '-'} previous={r.get('previous') or '-'} releases=[{rel}]")
    return OK


def main(argv=None):
    p = argparse.ArgumentParser(prog="rkdeploy",
                                description="Deploy RKNN apps to Rockchip boards as systemd services.")
    p.add_argument("--version", action="version", version=f"rkdeploy {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pack", help="pack an app directory into a deployable tarball")
    sp.add_argument("--manifest", default="rkdeploy.yaml")
    sp.add_argument("--out", default="dist")
    sp.set_defaults(fn=cmd_pack)

    for name, fn, helptext in (("deploy", cmd_deploy, "push a package to the fleet"),
                               ("rollback", cmd_rollback, "roll boards back to a previous release")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--fleet", required=True)
        sp.add_argument("--hosts", default=None, help="comma aliases or tag:<tag>")
        sp.add_argument("--jobs", type=int, default=4)
        if name == "deploy":
            sp.add_argument("package")
            sp.add_argument("--no-auto-rollback", action="store_true")
            sp.add_argument("--dry-run", action="store_true",
                            help="preview deployment without contacting boards")
        else:
            sp.add_argument("--app", required=True)
            sp.add_argument("--to", default=None)
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("status", help="fleet status report")
    sp.add_argument("--fleet", required=True)
    sp.add_argument("--app", required=True)
    sp.add_argument("--hosts", default=None)
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--json", default=None)
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("releases", help="list installed releases per board")
    sp.add_argument("--fleet", required=True)
    sp.add_argument("--app", required=True)
    sp.add_argument("--hosts", default=None)
    sp.add_argument("--jobs", type=int, default=4)
    sp.set_defaults(fn=cmd_releases)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (ManifestError, FleetError) as e:
        print(f"error: {e}", file=sys.stderr)
        return USAGE
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return USAGE


if __name__ == "__main__":
    sys.exit(main())
