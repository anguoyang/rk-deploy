# Contributing

rk-deploy is in its specification phase; v0.1 implementation starts
the week of Nov 9, 2026. The most useful contributions right now:

- **Review the spec** ([docs/SPEC.md](docs/SPEC.md)) — open an issue
  for anything in the manifest format, deploy sequence, rollback
  semantics or status report that would not work on your boards.
- **Board reports** — if you run RK3576 / RK356x boards, the thermal
  zone layouts and stock-library situations there are exactly what the
  preflight rules need to hear about.

House rules, non-negotiable:

- The tool stays generic: no application-specific presets, templates
  or sample payloads will be accepted.
- The board side stays dependency-free (POSIX sh + coreutils +
  systemd).
- Real-hardware numbers or it didn't happen: claims about boards need
  a reproducible measurement, not a datasheet.
