"""RemoteView daemon package: screen capture + TLS streaming.

On Linux a single foreground process does both (used for dev/testing here).
On Windows the work is split across two processes -- an un-killable Session-0
"Boss" service (streamer + supervisor) and a per-user-session "Helper"
(capture) -- because Session 0 isolation prevents a service from seeing the
interactive desktop. See supervisor.py and service/windows.py.
"""
