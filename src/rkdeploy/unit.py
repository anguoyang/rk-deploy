"""systemd unit template rendering (done at pack time, with
placeholders; the board installer substitutes @INSTALL_ROOT@,
@APP_NAME@, @APP_VERSION@ and @RUN_USER@ at install time because
those are board-level facts)."""

INTERPRETERS = {"python": "/usr/bin/python3 ", "shell": "/bin/sh ", "binary": ""}


def render_unit_template(m):
    svc = m["service"]
    entry = m["runtime"]["entry"]
    exec_start = f"{INTERPRETERS[m['runtime']['type']]}@INSTALL_ROOT@/current/{entry}"
    lines = [
        "[Unit]",
        f"Description=rkdeploy managed app: @APP_NAME@ @APP_VERSION@",
        f"# {m['description']}" if m["description"] else "# rkdeploy",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        "User=@RUN_USER@",
        "WorkingDirectory=@INSTALL_ROOT@/current",
        f"ExecStart={exec_start}",
        "Environment=RKDEPLOY_SHARED=@INSTALL_ROOT@/shared",
        "Environment=RKDEPLOY_VERSION=@APP_VERSION@",
    ]
    for k in sorted(svc["env"]):
        lines.append(f"Environment={k}={svc['env'][k]}")
    lines += [
        "EnvironmentFile=-@INSTALL_ROOT@/shared/config/@APP_NAME@.env",
        f"Restart={svc['restart']}",
        f"RestartSec={svc['restart_sec']}",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)
