"""Event-driven Tk speech indicator: no frame loop while hidden."""

import math
import os
import threading
import time
import tkinter as tk


class CrossOverlayController:
    def __init__(self):
        self.root = None
        self.parent = None
        self.canvas = None
        self.active_until = 0.0
        self.job = None
        self.enabled = True
        self.owner_thread = None

    def attach(self, parent):
        if self.root is not None:
            return
        self.parent = parent
        self.owner_thread = threading.get_ident()
        self.root = tk.Toplevel(parent)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        colour = getattr(parent, "theme", {}).get("sidebar", "#111520")
        if os.name == "nt":
            colour = "#000001"
            self.root.attributes("-transparentcolor", colour)
        self.root.configure(bg=colour)
        x = (parent.winfo_screenwidth() - 50) // 2
        y = parent.winfo_screenheight() - 100
        self.root.geometry(f"50x65+{x}+{y}")
        self.canvas = tk.Canvas(self.root, width=50, height=65, bg=colour, highlightthickness=0)
        self.canvas.pack()
        from PIL import ImageTk
        from app_icons import load_image

        self.art = ImageTk.PhotoImage(load_image(48), master=self.root)

    def _animate(self):
        self.job = None
        if self.root is None:
            return
        if not self.enabled or time.monotonic() >= self.active_until:
            self.root.withdraw()
            return
        self.root.deiconify()
        self.canvas.delete("all")
        reduced = (
            self.parent.config_mgr.snapshot().get("accessibility", {}).get("reduce_motion", False)
            if hasattr(self.parent, "config_mgr")
            else False
        )
        width = 3 if reduced else 3 + math.sin(time.monotonic() * 5)
        colour = getattr(self.parent, "theme", {}).get("accent", "#a78bfa")
        self.canvas.create_oval(4, 5, 46, 59, outline=colour, width=width / 2)
        self.canvas.create_image(25, 32, image=self.art)
        delay = max(1, int((self.active_until - time.monotonic()) * 1000)) if reduced else 40
        self.job = self.root.after(delay, self._animate)

    def pulse(self, duration=3):
        if self.root is None:
            return
        if threading.get_ident() != self.owner_thread:
            # The app queue is thread safe; Tk is only touched on its owner thread.
            if hasattr(self.parent, "post_ui"):
                self.parent.post_ui(lambda: self.pulse(duration))
            return
        self.active_until = max(self.active_until, time.monotonic() + max(0, duration))
        if self.job is None:
            self._animate()

    def stop(self):
        if self.root is not None:
            if self.job:
                self.root.after_cancel(self.job)
            self.root.destroy()
        self.root = self.parent = self.canvas = self.job = None
        self.art = None
        self.active_until = 0


indicator_controller = CrossOverlayController()


def trigger_indicator_pulse(duration=3):
    indicator_controller.pulse(duration)
