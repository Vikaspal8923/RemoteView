"""Linux systemd installer for the RemoteView daemon.

Writes and enables a systemd unit at /etc/systemd/system/remoteview.service.
The daemon runs as root (no User= line), so a normal-user process cannot
force-close it -- only an admin can (`systemctl stop remoteview`). It stays
visible in `systemctl status` / Task Manager-equivalents (by design -- it is
protected, not hidden). `Restart=always` self-heals crashes.

On Linux the single-process foreground daemon can capture the X/Wayland screen
directly when given the right DISPLAY/XAUTHORITY (there is no Session-0
isolation on Linux), so no separate Helper process is needed here.

Install:  sudo python -m daemon.service.linux install --config /etc/remoteview/config.json
Remove:   sudo python -m daemon.service.linux remove
Status:   sudo python -m daemon.service.linux status
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

SERVICE_NAME = "remoteview"
UNIT_NAME = f"{SERVICE_NAME}.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"

UNIT_TEMPLATE = """\
[Unit]
Description=RemoteView screen monitoring daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# No User= line -> runs as root. A normal-user process cannot stop it;
# only an admin can (systemctl stop remoteview). Visible by design.
ExecStart={python} -m daemon.main --config {config} --foreground
WorkingDirectory={workdir}
Environment=REMOTEVIEW_CONFIG={config}
Environment=DISPLAY={display}
Environment=XAUTHORITY={xauthority}
Restart=always
RestartSec=1
# Self-heal: restart on crash / unprivileged kill attempts.
KillMode=mixed
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


class LinuxServiceError(RuntimeError):
    pass


def _require_root():
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise LinuxServiceError(
            "must run as root (try: sudo python -m daemon.service.linux ...)"
        )


def _run(cmd, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise LinuxServiceError(
            f"command failed: {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result


def render_unit(
    config_path: str,
    python_exe: str = sys.executable,
    workdir: str = os.getcwd(),
    display: str = ":0",
    xauthority: str = "/run/user/1000/gdm/Xauthority",
) -> str:
    """Return the systemd unit file contents (pure function, testable)."""
    return UNIT_TEMPLATE.format(
        python=python_exe,
        config=config_path,
        workdir=workdir,
        display=display,
        xauthority=xauthority,
    )


def install(config_path: str, display: str = ":0",
            xauthority: str = "/run/user/1000/gdm/Xauthority") -> None:
    """Write the unit, reload systemd, and enable+start the service."""
    _require_root()
    unit = render_unit(config_path, display=display, xauthority=xauthority)
    with open(UNIT_PATH, "w", encoding="utf-8") as fh:
        fh.write(unit)
    os.chmod(UNIT_PATH, 0o644)
    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", UNIT_NAME])
    _run(["systemctl", "restart", UNIT_NAME])
    print(f"Installed and started {UNIT_NAME}")
    print(f"  status:  systemctl status {UNIT_NAME}")
    print(f"  stop:    sudo systemctl stop {UNIT_NAME}")


def remove() -> None:
    """Stop, disable, and delete the unit."""
    _require_root()
    _run(["systemctl", "stop", UNIT_NAME], check=False)
    _run(["systemctl", "disable", UNIT_NAME], check=False)
    try:
        os.remove(UNIT_PATH)
    except FileNotFoundError:
        pass
    _run(["systemctl", "daemon-reload"])
    print(f"Removed {UNIT_NAME}")


def status() -> None:
    _run(["systemctl", "--no-pager", "status", UNIT_NAME], check=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RemoteView Linux service")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install")
    p_install.add_argument("--config", required=True)
    p_install.add_argument("--display", default=":0")
    p_install.add_argument("--xauthority", default="/run/user/1000/gdm/Xauthority")

    sub.add_parser("remove")
    sub.add_parser("status")

    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            install(args.config, display=args.display, xauthority=args.xauthority)
        elif args.command == "remove":
            remove()
        elif args.command == "status":
            status()
    except LinuxServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
