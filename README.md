# rk-deploy

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Ship RKNN apps to a fleet of Rockchip boards like you mean it:**
describe the app in one manifest → one command packs it → it lands on every
board as a systemd service → push the next version, roll back in seconds,
and get a fleet status report you can trust.

rk-deploy is a deployment and small-fleet tool for RKNN applications on
RK3588 / RK3576 / RK356x boards, built for the people who deliver and
maintain them: display vendors, parking and security integrators, and
anyone currently copying tarballs over scp and hand-writing `.service`
files per board.

## Status

**Specification frozen, feasibility validated on real hardware — code in
development.** v0.1 is scheduled for the week of Nov 9, 2026. This
repository currently contains the design specification
([docs/SPEC.md](docs/SPEC.md)) so the format and the rules can be reviewed
and discussed before the implementation lands.

The full flow — pack → push over SSH → install as a systemd service →
push v2 → roll back to v1 → fleet status snapshot — has already been
walked end-to-end by hand on an RK3588 board (Ubuntu 22.04, systemd 249):
105 seconds including cleanup, and every pitfall found on the way is
written into the spec (SSH connection overhead dominates everything,
board-side YAML parsing is unreliable, `ln -sfn` on a directory symlink
is a trap, a whole-machine `degraded` systemd state says nothing about
your app).

## How it will work

```yaml
# rkdeploy.yaml
name: demo
version: 1.1.0
runtime:
  type: python
  entry: app.py
models:
  - path: models/weights.rknn
healthcheck:
  type: file
  path: "@INSTALL_ROOT@/shared/heartbeat.json"
  timeout_sec: 15
```

```sh
rkdeploy pack                                  # → demo-1.1.0.tar.gz (checksummed)
rkdeploy deploy --fleet fleet.yaml demo-1.1.0.tar.gz
rkdeploy rollback --fleet fleet.yaml --app demo
rkdeploy status --fleet fleet.yaml             # JSON + per-board summary
```

Design rules that will not change:

- **Generic packages only.** rk-deploy knows nothing about the app it
  ships. Model files are transported as opaque bytes and never parsed.
- **Immutable releases + atomic switch.** Each version unpacks into its
  own directory; going live (or rolling back) is a single symlink swap.
- **Data outlives versions.** Config and data live in a `shared/`
  directory that upgrades and rollbacks never touch.
- **Failed health check = automatic rollback** to the previous version,
  with an explicit verdict — never a half-installed board.
- **No agent, no new ports, no credential collection.** Plain SSH/scp
  with your existing keys; one connection per board per operation.
- **The board stays dumb.** All parsing happens on the controller; the
  board runs one POSIX shell installer and stock systemd.

## Roadmap

- **v0.1** (week of Nov 9, 2026): `pack` / `deploy` / `rollback` /
  `status` / `releases`, fleet push over SSH, automatic rollback.
- **v0.2**: fleet HTML status report, optional resumable transfer for
  large model packages.

## Related

Part of a small family of open Rockchip edge tools by
[Site AI Gateway](https://site-ai-gateway.com):
[rk-edge-doctor](https://github.com/anguoyang/rk-edge-doctor) (board /
model / RTSP diagnostics) ·
[rk-bench](https://github.com/anguoyang/rk-bench) (measured RKNN model
leaderboard) ·
[awesome-rockchip](https://github.com/anguoyang/awesome-rockchip).

## License

Apache-2.0. See [LICENSE](LICENSE).
