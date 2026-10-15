"""Small Windows-only display-affinity test window."""
from __future__ import annotations
import argparse
import ctypes
import tkinter as tk

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wda-monitor", action="store_true")
    args = parser.parse_args()
    if not hasattr(ctypes, "windll"):
        raise SystemExit("This harness requires Windows")
    root = tk.Tk(); root.title("RemoteView protected window")
    tk.Label(root, text="RemoteView affinity capture test", font=("Segoe UI", 24), padx=50, pady=50).pack()
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
    affinity = 0x01 if args.wda_monitor else 0x11
    if not ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, affinity):
        raise ctypes.WinError(ctypes.get_last_error())
    print(f"HWND={hwnd} AFFINITY={affinity}", flush=True)
    root.mainloop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
