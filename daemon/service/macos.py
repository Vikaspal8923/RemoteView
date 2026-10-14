"""macOS launchd installer for the RemoteView daemon.

Per the design doc: writes and loads a launchd plist at
~/Library/LaunchAgents/com.remoteview.daemon.plist. As with the Linux unit, the
daemon runs foreground/headless (no window) and is visible in `launchctl list`.

Manage after install:
    launchctl stop com.remoteview.daemon
    launchctl start com.remoteview.daemon
    launchctl unload ~/Library/LaunchAgents/com.remoteview.daemon.plist

Install:  python -m daemon.service.macos install --config <path>
Remove:   python -m daemon.service.macos remove
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

LABEL = "com.remoteview.daemon"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>daemon.main</string>
        <string>--config</string>
        <string>{config}</string>
        <string>--foreground</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{workdir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{logdir}/remoteview.out.log</string>
    <key>StandardErrorPath</key>
    <string>{logdir}/remoteview.err.log</string>
</dict>
</plist>
"""


class MacOSServiceError(RuntimeError):
    pass


def render_plist(config_path: str, python_exe: str = sys.executable,
                 workdir: str = os.getcwd(),
                 logdir: str = os.path.expanduser("~/Library/Logs/RemoteView")) -> str:
    """Return the launchd plist contents (pure function, testable)."""
    return PLIST_TEMPLATE.format(
        label=LABEL,
        python=python_exe,
        config=config_path,
        workdir=workdir,
        logdir=logdir,
    )


def _run(cmd, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise MacOSServiceError(
            f"command failed: {' '.join(cmd)}\n{result.stderr.strip()}"
        )
    return result


def install(config_path: str) -> None:
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "w", encoding="utf-8") as fh:
        fh.write(render_plist(config_path))
    _run(["launchctl", "load", PLIST_PATH])
    print(f"Loaded {LABEL}")
    print(f"  stop:  launchctl stop {LABEL}")


def remove() -> None:
    _run(["launchctl", "unload", PLIST_PATH], check=False)
    try:
        os.remove(PLIST_PATH)
    except FileNotFoundError:
        pass
    print(f"Removed {LABEL}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RemoteView macOS service")
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install")
    p_install.add_argument("--config", required=True)
    sub.add_parser("remove")
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            install(args.config)
        elif args.command == "remove":
            remove()
    except MacOSServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
