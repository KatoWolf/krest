"""Local Tk fallback/preview of Relic V2; Windows runtime uses its own process."""

import os
import threading
import time
import tkinter as tk

from relic_indicator import ListeningAnimation


class CrossOverlayController:
    def __init__(self):
        self.root = self.parent = self.canvas = self.job = None
        self.enabled = True
        self.owner_thread = None
        self.active_until = 0
        self.phase = ListeningAnimation()
        self.feedback = {}
        self.art = None
        self._last_frame = None
        self._native_owner = False

    def attach(self, parent):
        if self.root is not None or self._native_owner:
            return
        self.parent = parent
        self.owner_thread = threading.get_ident()
        if os.name == "nt" and hasattr(getattr(parent, "runtime", None), "client"):
            self._native_owner = True
            return
        self.root = tk.Toplevel(parent)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        colour = getattr(parent, "theme", {}).get("sidebar", "#111520")
        self.root.configure(bg=colour)
        x = (parent.winfo_screenwidth() - 170) // 2
        y = parent.winfo_screenheight() - 180
        self.root.geometry(f"170x152+{x}+{y}")
        self.canvas = tk.Canvas(self.root, width=170, height=152, bg=colour, highlightthickness=0)
        self.canvas.pack()
        self.item = self.canvas.create_image(85, 76)

    def show_feedback(self, feedback, enabled=True):
        if self._native_owner or self.root is None:
            return
        self.feedback, self.enabled = dict(feedback), enabled
        visible = self.phase.sync(self.feedback, enabled=enabled)
        if not visible:
            self.root.withdraw()
        elif self.job is None:
            self._animate()

    def _animate(self):
        self.job = None
        if self.root is None:
            return
        if not self.phase.sync(self.feedback, enabled=self.enabled):
            self.root.withdraw()
            return
        reduced = (
            self.parent.config_mgr.snapshot().get("accessibility", {}).get("reduce_motion", False)
            if hasattr(self.parent, "config_mgr")
            else False
        )
        frame = self.phase.frame(reduced=reduced)
        if frame is not self._last_frame:
            from PIL import ImageTk

            self._last_frame = frame
            self.art = ImageTk.PhotoImage(frame, master=self.root)
            self.canvas.itemconfigure(self.item, image=self.art)
        self.root.deiconify()
        self.job = self.root.after(50, self._animate)

    def pulse(self, duration=3):
        if self.root is None:
            return
        if threading.get_ident() != self.owner_thread:
            if hasattr(self.parent, "post_ui"):
                self.parent.post_ui(lambda: self.pulse(duration))
            return
        self.active_until = time.monotonic() + max(0, duration)
        self.show_feedback(
            {
                "turn": self.phase.turn + 1,
                "source": "voice",
                "stage": "listening",
                "until": time.time() + max(0, duration),
            }
        )

    def stop(self):
        if self.root is not None:
            if self.job:
                self.root.after_cancel(self.job)
            self.root.destroy()
        self.root = self.parent = self.canvas = self.job = self.art = None
        self._last_frame = None
        self._native_owner = False
        self.active_until = 0
        self.phase = ListeningAnimation()


indicator_controller = CrossOverlayController()


def trigger_indicator_pulse(duration=3):
    indicator_controller.pulse(duration)
