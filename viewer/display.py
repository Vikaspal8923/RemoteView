"""tkinter frame renderer for the viewer.

Runs on the main thread (tkinter requirement). Reads decoded frames from a
thread-safe queue (fed by the asyncio receiver running in a background
thread), converts each PIL.Image to ImageTk.PhotoImage, and draws it on a
canvas sized to the received frame. The title bar shows end-to-end latency
(unix_ms delta) and a smoothed FPS estimate. Q or window-close -> clean exit.
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from typing import Optional

from PIL import Image, ImageTk

# Poll interval (ms) for draining the frame queue on the tkinter event loop.
POLL_INTERVAL_MS = 15


class ViewerDisplay:
    """A tkinter window that continuously renders incoming frames."""

    def __init__(self, frame_queue: "queue.Queue", title: str = "RemoteView", control_send=None):
        self.frame_queue = frame_queue
        self.title = title
        self.control_send = control_send
        # Enabled by default for the current testing build. Escape still
        # disables control immediately; F8 toggles it thereafter.
        self.control_enabled = True

        self.root = tk.Tk()
        self.root.title(self.title)
        self.root.geometry("1024x640")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        # Keyboard events belong to the viewer window even when the user last
        # clicked the canvas. Explicit focus also makes typing work on Linux
        # window managers that do not focus a Canvas automatically.
        self.canvas.focus_set()
        self.root.bind_all("<KeyPress>", self._on_key_down, add="+")
        self.root.bind_all("<KeyRelease>", self._on_key_up, add="+")

        # Current photo image kept alive to avoid GC; drawn via canvas image.
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._canvas_img_id: Optional[int] = None

        # Stats for the title bar.
        self._last_latency_ms: Optional[int] = None
        # Windows and Linux clocks may differ by seconds. Calibrate the wall
        # clock offset on the first frame so the title does not report a huge
        # fake latency caused by unsynchronised system clocks.
        self._clock_offset_ms: Optional[int] = None
        self._fps_ema: Optional[float] = None
        self._last_frame_time: Optional[float] = None
        self._frames_since_update = 0
        self._last_title_update = time.monotonic()

        self._stop_requested = False

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<q>", self._on_q)
        self.root.bind("<Q>", self._on_q)
        if self.control_send:
            self.control_send("CONTROL_ENABLE")
        self.root.title(f"{self.title} | CONTROL ACTIVE")
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress>", self._on_button_down)
        self.canvas.bind("<ButtonRelease>", self._on_button_up)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    # ---- event handlers ---- #
    def _on_close(self):
        self._stop_requested = True
        self.root.destroy()

    def _on_q(self, _event=None):
        self._stop_requested = True
        self.root.destroy()

    def _on_emergency_stop(self, _event=None):
        self.control_enabled = False
        if self.control_send:
            self.control_send("EMERGENCY_STOP")
        self.root.title(f"{self.title} | MONITORING ONLY")
        return "break"

    def _send_control(self, kind, **data):
        if self.control_enabled and self.control_send:
            self.control_send(kind, **data)

    def _on_key_down(self, event):
        if event.keysym == "Escape":
            return self._on_emergency_stop(event)
        if event.keysym.lower() == "f8":
            self.control_enabled = not self.control_enabled
            if self.control_send:
                self.control_send("CONTROL_ENABLE" if self.control_enabled else "CONTROL_DISABLE")
            self.root.title(f"{self.title} | {'CONTROL ACTIVE' if self.control_enabled else 'MONITORING ONLY'}")
            return "break"
        if event.keysym not in ("q", "Q"):
            self._send_control("KEY_DOWN", key=event.keysym)

    def _on_key_up(self, event):
        if event.keysym not in ("q", "Q", "F8"):
            self._send_control("KEY_UP", key=event.keysym)

    def _coords(self, event):
        # The frame is aspect-fitted and centered in the canvas. Convert from
        # canvas coordinates to normalized source-image coordinates, excluding
        # the letterbox bars.
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        iw = self._photo.width() if self._photo is not None else cw
        ih = self._photo.height() if self._photo is not None else ch
        ox, oy = (cw - iw) / 2.0, (ch - ih) / 2.0
        return (max(0.0, min(1.0, (event.x - ox) / max(1, iw))),
                max(0.0, min(1.0, (event.y - oy) / max(1, ih))))

    def _on_motion(self, event):
        x, y = self._coords(event); self._send_control("MOUSE_MOVE", x=x, y=y)

    def _on_button_down(self, event):
        self.canvas.focus_set()
        x, y = self._coords(event); self._send_control("MOUSE_MOVE", x=x, y=y)
        self._send_control("MOUSE_BUTTON_DOWN", button={1:"left",2:"middle",3:"right"}.get(event.num,"left"))

    def _on_button_up(self, event):
        self._send_control("MOUSE_BUTTON_UP", button={1:"left",2:"middle",3:"right"}.get(event.num,"left"))

    def _on_wheel(self, event):
        self._send_control("MOUSE_SCROLL", delta=max(-20, min(20, int(event.delta / 120))))

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    # ---- frame pump ---- #
    def _drain_queue(self):
        """Pull all currently-available frames; render the newest one."""
        latest: Optional[tuple] = None
        drained = 0
        while True:
            try:
                latest = self.frame_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break

        if latest is not None:
            img, ts_ms = latest
            self._render(img, ts_ms)

        # Reschedule. Rendering the newest frame (not every queued frame) keeps
        # the UI smooth even when frames arrive faster than we can draw.
        self.root.after(POLL_INTERVAL_MS, self._drain_queue)

    def _render(self, img: Image.Image, ts_ms: int):
        now = time.time()
        now_ms = int(now * 1000)

        # Latency = now - capture time. Clamp tiny negatives (clock skew).
        if self._clock_offset_ms is None:
            self._clock_offset_ms = now_ms - ts_ms
        latency = max(0, now_ms - ts_ms - self._clock_offset_ms)
        self._last_latency_ms = latency

        # FPS via exponential moving average of inter-frame time.
        if self._last_frame_time is not None:
            inst_fps = 1.0 / max(1e-6, now - self._last_frame_time)
            self._fps_ema = (
                inst_fps if self._fps_ema is None else (0.8 * self._fps_ema + 0.2 * inst_fps)
            )
        self._last_frame_time = now
        self._frames_since_update += 1

        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            cw, ch = img.width, img.height

        # Fit the stream to the viewer window while preserving the source
        # aspect ratio. Do this on the already-downscaled frame so the viewer
        # remains responsive even when the window is large.
        scale = min(cw / img.width, ch / img.height)
        draw_w = max(1, int(img.width * scale))
        draw_h = max(1, int(img.height * scale))
        if draw_w != img.width or draw_h != img.height:
            img = img.resize((draw_w, draw_h), Image.Resampling.BILINEAR)

        # Convert to PhotoImage and draw.
        self._photo = ImageTk.PhotoImage(img)
        if self._canvas_img_id is None:
            self._canvas_img_id = self.canvas.create_image(
                cw // 2, ch // 2, image=self._photo, anchor=tk.CENTER
            )
        else:
            self.canvas.coords(self._canvas_img_id, cw // 2, ch // 2)
            self.canvas.itemconfig(self._canvas_img_id, image=self._photo)

        # Throttle title updates to ~2/sec to avoid churn.
        if now - self._last_title_update >= 0.5:
            fps_txt = f"{self._fps_ema:.1f} fps" if self._fps_ema else "-- fps"
            lat_txt = f"{latency} ms" if self._last_latency_ms is not None else "-- ms"
            state_txt = "CONTROL ACTIVE" if self.control_enabled else "MONITORING ONLY"
            self.root.title(f"{self.title}  |  {state_txt}  |  latency {lat_txt}  |  {fps_txt}")
            self._last_title_update = now

    def run(self):
        """Start the tkinter event loop (blocks until the window closes)."""
        self.root.after(POLL_INTERVAL_MS, self._drain_queue)
        self.root.mainloop()
