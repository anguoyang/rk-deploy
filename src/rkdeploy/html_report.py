"""Render schema-1 status reports as standalone, offline HTML."""
from html import escape


_CSS = """
body { margin: 0; background: #f3f5f7; color: #1f2937;
       font-family: system-ui, sans-serif; line-height: 1.5; }
main { max-width: 1000px; margin: auto; padding: 24px; }
header, article { background: white; border: 1px solid #d1d5db;
                  border-radius: 10px; padding: 24px; margin-bottom: 20px; }
h1, h2, h3 { margin: 0 0 12px; }
.verdict { display: inline-block; padding: 4px 12px; border-radius: 6px;
           background: #e5e7eb; font-weight: bold; }
.healthy { background: #dcfce7; color: #166534; }
.attention { background: #fef3c7; color: #854d0e; }
.unhealthy, .unreachable { background: #fee2e2; color: #991b1b; }
dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
     gap: 12px 24px; }
dt { font-size: 0.85rem; color: #4b5563; }
dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
h2, td { overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid #d1d5db; }
@media print { body { background: white; } article { break-inside: avoid; } }
"""


def _value(value):
    """Escape report data while distinguishing absent values from false/zero."""
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(_value(item) for item in value) or "-"
    return escape(str(value), quote=True)


def _field(label, value):
    return f"<div><dt>{escape(label)}</dt><dd>{_value(value)}</dd></div>"


def _verdict(value):
    # Only known verdicts may contribute a CSS class; data stays in text nodes.
    style = value.lower() if value in (
        "HEALTHY", "ATTENTION", "UNHEALTHY", "UNREACHABLE",
    ) else ""
    return f'<span class="verdict {style}">{_value(value)}</span>'


def render_html_report(report):
    """Return HTML for the same report object used by --json, without mutating it.

    Library names are intentionally omitted: only system_library_count is shown.
    Missing values (including fields absent from older schema-1 reports) use '-'.
    """
    parts = [
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>rk-deploy fleet status report</title>',
        f'<style>{_CSS}</style>\n</head>\n<body>\n<main>\n<header>',
        '<h1>Fleet status report</h1>',
        f'<p>Fleet verdict: {_verdict(report.get("fleet_verdict"))}</p>',
        '<dl>' + ''.join(_field(key, report.get(key)) for key in (
            "generated_at", "tool_version", "schema",
        )) + '</dl>\n</header>',
    ]
    for board in report.get("fleet") or []:
        parts.extend([
            '<article>',
            f'<h2>{_value(board.get("alias"))}</h2>',
            f'<p>Board verdict: {_verdict(board.get("verdict"))}</p>',
            '<dl>',
        ])
        for key in (
            "alias", "reachable", "installed", "version", "previous", "service",
            "enabled", "main_pid", "service_since", "releases", "disk_free_pct",
        ):
            parts.append(_field(key, board.get(key)))
        temps = board.get("temps_c") or {}
        for key in ("soc", "max"):
            parts.append(_field(f"temps_c.{key} (°C)", temps.get(key)))
        for key in (
            "systemd_version", "system_library_count", "health",
            "journal_errors_1h", "version_drift",
        ):
            parts.append(_field(key, board.get(key)))
        if "probe_error" in board:
            parts.append(_field("probe_error", board.get("probe_error")))
        parts.append('</dl>\n<h3>thermal_zones</h3>')
        zones = board.get("thermal_zones") or []
        if zones:
            parts.append('<table><thead><tr><th scope="col">name</th>'
                         '<th scope="col">type</th><th scope="col">temp_c (°C)</th>'
                         '</tr></thead><tbody>')
            for zone in zones:
                parts.append('<tr>' + ''.join(
                    f'<td>{_value(zone.get(key))}</td>'
                    for key in ("name", "type", "temp_c")
                ) + '</tr>')
            parts.append('</tbody></table>')
        else:
            parts.append('<p>-</p>')
        parts.append('</article>')
    if not report.get("fleet"):
        parts.append('<p>Boards: -</p>')
    parts.append('</main>\n</body>\n</html>\n')
    return '\n'.join(parts)
