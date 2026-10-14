"""Boss supervisor: launch the capture Helper into the interactive session and
respawn it if it dies (Windows only).

Why this exists: Session 0 isolation means the un-killable Boss service cannot
capture the interactive desktop. The Helper must run *in the user's session* to
see the screen. This module implements the Microsoft-recommended launch pattern:

    WTSGetActiveConsoleSessionId()   -> find the active user's session id
    WTSQueryUserToken(session_id)    -> get that user's primary token
    DuplicateTokenEx(...)            -> make a usable primary token
    CreateEnvironmentBlock(...)      -> build the user's environment
    CreateProcessAsUser(...)         -> start the Helper in that session

The Helper writes frames to a named pipe (see daemon/pipe.py) that the Boss
reads. If the Helper exits (killed by the user or a crash), the Boss restarts
it after a short delay -- the auto-respawn backstop discussed with the user.
The Boss itself holds the auth token + TLS and is un-killable by a normal user.

All Windows imports are lazy so this module imports cleanly on Linux/macOS
(the functions raise SupervisorError if actually called off Windows).
"""

from __future__ import annotations

import os
import sys
import time
from typing import List, Optional

from shared.config import Config

# Delay before respawning a dead Helper. Short enough that the stream recovers
# quickly; a constant keeps behavior predictable.
RESPAWN_DELAY_SECONDS = 1.0


class SupervisorError(RuntimeError):
    """Raised when the Helper cannot be launched into the user session."""


def _load_win_modules():
    """Import the Windows APIs; raise clearly off Windows."""
    if not sys.platform.startswith("win"):
        raise SupervisorError("supervisor is Windows-only")
    try:
        import win32api
        import win32con
        import win32event
        import win32process
        import win32profile
        import win32security
        import win32ts
        import pywintypes
    except Exception as exc:  # ModuleNotFoundError / ImportError
        raise SupervisorError(
            f"pywin32 (and its modules) are required on Windows: {exc}"
        ) from exc
    return {
        "win32api": win32api,
        "win32con": win32con,
        "win32event": win32event,
        "win32process": win32process,
        "win32profile": win32profile,
        "win32security": win32security,
        "win32ts": win32ts,
        "pywintypes": pywintypes,
    }


def get_active_session_id() -> int:
    """Return the session id of the user attached to the physical console."""
    mods = _load_win_modules()
    win32ts = mods["win32ts"]
    session_id = win32ts.WTSGetActiveConsoleSessionId()
    # 0xFFFFFFFF (INVALID_SESSION_ID) means no interactive user is logged on.
    if session_id == 0xFFFFFFFF:
        raise SupervisorError("no active console session (nobody logged in)")
    return session_id


def _build_user_primary_token(mods, session_id: int):
    """Query + duplicate the interactive user's primary token."""
    win32ts = mods["win32ts"]
    win32security = mods["win32security"]
    win32con = mods["win32con"]

    user_token = win32ts.WTSQueryUserToken(session_id)
    primary = win32security.DuplicateTokenEx(
        user_token,
        win32con.MAXIMUM_ALLOWED,
        None,
        win32security.SecurityImpersonation,
        win32security.TokenPrimary,
    )
    return primary


def launch_helper(cfg: Config) -> int:
    """Launch the capture Helper in the active user's session.

    Returns the Helper's process handle (an int the caller can wait on). The
    Helper is started as:  python -m daemon.main --config <cfg> --worker
    using the same interpreter that is running the Boss (sys.executable), so we
    don't depend on PATH resolution.
    """
    mods = _load_win_modules()
    win32process = mods["win32process"]
    win32con = mods["win32con"]
    win32event = mods["win32event"]

    session_id = get_active_session_id()
    primary_token = _build_user_primary_token(mods, session_id)

    # Command line for the Helper. Quote the config path in case of spaces.
    config_path = cfg.config_path or "config.json"
    cmd = f'"{sys.executable}" -m daemon.main --config "{config_path}" --worker'

    # Environment block for the interactive user (so the Helper sees the right
    # DISPLAY-equivalent, profile, etc.).
    try:
        env_block = mods["win32profile"].CreateEnvironmentBlock(primary_token, False)
    except Exception:
        env_block = None

    startup = win32process.STARTUPINFO()
    startup.dwFlags = win32process.STARTF_USESHOWWINDOW
    startup.wShowWindow = win32con.SW_HIDE  # Helper has no window

    # CREATE_NO_WINDOW keeps the Helper windowless (belt-and-suspenders with the
    # capture loop itself never opening a GUI).
    creation_flags = win32con.CREATE_NO_WINDOW | win32con.NORMAL_PRIORITY_CLASS

    try:
        _hp, _ht, pid, _tid = win32process.CreateProcessAsUser(
            primary_token,
            None,               # application name (use cmd line)
            cmd,                # command line
            None,               # process attributes
            None,               # thread attributes
            0,                  # inherit handles
            creation_flags,
            env_block,
            None,               # current directory
            startup,
        )
    except Exception as exc:  # pywintypes.error and friends
        raise SupervisorError(f"CreateProcessAsUser failed: {exc}") from exc

    return pid


def supervise(cfg: Config, should_continue=None) -> None:
    """Launch the Helper and keep it alive (respawn on exit) forever.

    Blocks. ``should_continue()`` (optional) is polled between respawns so the
    service can stop the loop on shutdown; when it returns False we exit.

    On Linux/non-Windows this raises SupervisorError (the single-process
    foreground mode is used there instead).
    """
    if should_continue is None:
        def should_continue() -> bool:
            return True

    mods = _load_win_modules()
    win32api = mods["win32api"]
    win32event = mods["win32event"]
    win32con = mods["win32con"]

    # Import psutil lazily to wait on the Helper pid cross-check; we actually
    # use the OS process handle via OpenProcess + WaitForSingleObject.
    import psutil  # cross-platform, already a dependency

    while should_continue():
        try:
            pid = launch_helper(cfg)
        except SupervisorError as exc:
            print(f"[supervisor] launch failed: {exc}; retrying", flush=True)
            time.sleep(RESPAWN_DELAY_SECONDS)
            continue

        print(f"[supervisor] Helper started (pid={pid})", flush=True)

        # Open a waitable handle for the Helper process and block until it
        # exits, then loop to respawn. We poll should_continue periodically.
        try:
            proc_handle = win32api.OpenProcess(
                win32con.SYNCHRONIZE | win32con.PROCESS_QUERY_INFORMATION,
                False,
                pid,
            )
        except Exception as exc:
            print(f"[supervisor] OpenProcess failed: {exc}", flush=True)
            time.sleep(RESPAWN_DELAY_SECONDS)
            continue

        # Wait for the Helper to exit, waking every 0.5s to check should_continue.
        while True:
            rc = win32event.WaitForSingleObject(proc_handle, 500)
            if rc == win32con.WAIT_OBJECT_0:
                print(f"[supervisor] Helper (pid={pid}) exited; respawning", flush=True)
                break
            if not should_continue():
                print("[supervisor] stop requested; terminating Helper", flush=True)
                try:
                    psutil.Process(pid).terminate()
                except Exception:
                    pass
                break
            # rc == WAIT_TIMEOUT: keep waiting.

        if not should_continue():
            break
        time.sleep(RESPAWN_DELAY_SECONDS)

