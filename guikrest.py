"""A Tk overlay owned by the main Tk thread, with thread-safe pulse requests."""

import math
import os
import time
import tkinter as tk


class CrossOverlayController:
    def __init__(self):
        self.root = None
        self.canvas = None
        self.active_until = 0.0
        self.job = None
        self.enabled = True

    def attach(self, parent):
        if self.root is not None:
            return
        self.root = tk.Toplevel(parent)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        color = "#111520"
        if os.name == "nt":
            color = "#000001"
            self.root.attributes("-transparentcolor", color)
        self.root.configure(bg=color)
        x = (parent.winfo_screenwidth() - 50) // 2
        y = parent.winfo_screenheight() - 100
        self.root.geometry(f"50x65+{x}+{y}")
        self.canvas = tk.Canvas(self.root, width=50, height=65, bg=color, highlightthickness=0)
        self.canvas.pack()
        self._animate()

    def _animate(self):
        if self.root is None:
            return
        if self.enabled and time.monotonic() < self.active_until:
            self.root.deiconify()
            self.canvas.delete("all")
            width = 3 + math.sin(time.monotonic() * 5)
            self.canvas.create_line(25, 9, 25, 55, fill="#a78bfa", width=width)
            self.canvas.create_line(12, 24, 38, 24, fill="#a78bfa", width=width)
        else:
            self.root.withdraw()
        self.job = self.root.after(40, self._animate)

    def pulse(self, duration=3):
        self.active_until = max(self.active_until, time.monotonic() + max(0, duration))

    def stop(self):
        if self.root is not None:
            if self.job:
                self.root.after_cancel(self.job)
            self.root.destroy()
            self.root = None


indicator_controller = CrossOverlayController()


def trigger_indicator_pulse(duration=3):
    indicator_controller.pulse(duration)
