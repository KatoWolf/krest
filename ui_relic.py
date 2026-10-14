"""Relic identity components: static artwork, native controls and cached state."""

import tkinter as tk
from tkinter import ttk
from PIL import ImageTk
from app_icons import load_image, load_wordmark
from ui_fonts import UI_FONT
import math


class ThornVine(tk.Canvas):
    """A sparse, static border ornament. Redraw only when its size changes."""

    def __init__(self, parent, colours, vertical=False, **kwargs):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, takefocus=0, **kwargs)
        self.colours, self.vertical = colours, vertical
        self.job = None
        self.highlighted = False
        self.bind("<Configure>", self.schedule)
        self.bind("<Destroy>", self.cancel, add="+")

    def cancel(self, event=None):
        if self.job:
            self.after_cancel(self.job)
            self.job = None

    def schedule(self, event=None):
        if self.job is None:
            self.job = self.after_idle(self.draw)

    def highlight(self, active):
        if self.highlighted != active:
            self.highlighted = active
            self.schedule()

    def draw(self):
        self.job = None
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        length, breadth = (h, w) if self.vertical else (w, h)
        if length < 5:
            return
        colour = self.colours["accent"] if self.highlighted else self.colours["border"]

        def point(t, offset=0):
            across = breadth * 0.5 + math.sin(t / 33) * breadth * 0.18 + offset
            return (across, t) if self.vertical else (t, across)

        points = [point(t) for t in range(5, length - 4, 6)]
        if len(points) > 1:
            self.create_line(*[n for p in points for n in p], fill=colour, width=1.4, smooth=True)
        for index, t in enumerate(range(19, length - 15, 31)):
            sign = 1 if index % 2 else -1
            base, tip, end = point(t), point(t + 8, sign * breadth * 0.35), point(t + 6)
            self.create_polygon(*base, *tip, *end, fill=colour, outline="")
        # A second interrupted stem, quieter than the main vine.
        for t in range(32, length - 28, 104):
            pts = [
                point(t),
                point(t + 14, -breadth * 0.27),
                point(t + 33, breadth * 0.2),
                point(t + 49),
            ]
            self.create_line(*[n for p in pts for n in p], fill=self.colours["glow"], smooth=True)


class ThornButton(tk.Frame):
    """Native accessible button, with an ornament below its clickable surface."""

    def __init__(self, parent, colours, **kwargs):
        super().__init__(parent, bg=parent.cget("bg"))
        self.button = ttk.Button(self, **kwargs)
        self.button.pack(fill="x")
        self.vine = ThornVine(self, colours, width=1, height=13)
        self.vine.pack(fill="x")
        self.button.bind("<Enter>", lambda e: self.vine.highlight(True), add="+")
        self.button.bind("<Leave>", lambda e: self.vine.highlight(False), add="+")
        self.button.bind("<FocusIn>", lambda e: self.vine.highlight(True), add="+")
        self.button.bind("<FocusOut>", lambda e: self.vine.highlight(False), add="+")


class RelicEmblem(tk.Canvas):
    def __init__(self, parent, colours, size=144):
        super().__init__(
            parent, width=size, height=size, bg=parent.cget("bg"), highlightthickness=0
        )
        self.art = ImageTk.PhotoImage(load_image(size), master=self)
        edge = colours["border"]
        for x, y, dx, dy in (
            (8, 8, 1, 1),
            (size - 8, 8, -1, 1),
            (8, size - 8, 1, -1),
            (size - 8, size - 8, -1, -1),
        ):
            self.create_line(x, y + dy * 12, x, y, x + dx * 12, y, fill=edge)
        self.create_image(size / 2, size / 2, image=self.art)


def wordmark(parent, colours, width=156):
    from ui_design import contrasting_text

    tone = "black" if contrasting_text(parent.cget("bg")) != "#ffffff" else "metal"
    image = ImageTk.PhotoImage(load_wordmark(width, tone), master=parent)
    label = tk.Label(parent, image=image, bg=parent.cget("bg"), bd=0)
    label.image = image
    return label


class StateTile(tk.Frame):
    def __init__(self, parent, colours, title, command):
        super().__init__(
            parent,
            bg=colours["card"],
            highlightthickness=1,
            highlightbackground=colours["border"],
            padx=14,
            pady=10,
        )
        self.title = tk.Label(
            self,
            text=title,
            bg=colours["card"],
            fg=colours["sub_text"],
            font=(UI_FONT, 10),
            anchor="w",
        )
        self.title.pack(fill="x")
        self.value = ttk.Button(self, text="—", command=command, style="Tile.TButton")
        self.value.pack(fill="x", pady=(4, 0))
        self.previous = None

    def set(self, value):
        if value != self.previous:
            self.value.configure(text=value)
            self.previous = value


def theme_preview(parent, palette, width=220, height=100):
    """A tiny representation of the actual layout, without rebuilding a window."""
    canvas = tk.Canvas(parent, width=width, height=height, bg=palette["bg"], highlightthickness=0)
    canvas.create_rectangle(0, 0, 48, height, fill=palette["sidebar"], outline="")
    for y in (24, 40, 56, 72):
        canvas.create_rectangle(
            8, y, 39, y + 6, fill=palette["glow"] if y == 24 else palette["border"], outline=""
        )
    canvas.create_rectangle(58, 12, width - 10, 59, fill=palette["card"], outline=palette["border"])
    canvas.create_line(68, 26, 135, 26, fill=palette["text"], width=3)
    canvas.create_line(68, 37, 117, 37, fill=palette["sub_text"], width=2)
    canvas.create_rectangle(68, 46, 105, 51, fill=palette["accent"], outline="")
    for x in (58, 110, 162):
        canvas.create_rectangle(
            x, 68, x + 43, 88, fill=palette["card_hover"], outline=palette["border"]
        )
    return canvas
