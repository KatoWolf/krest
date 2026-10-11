"""Shared visual system. Explicit state colours avoid native white hover surfaces."""

from ui_fonts import UI_FONT, load_fonts

import time
import tkinter as tk
from tkinter import ttk


def contrasting_text(colour):
    rgb = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return (
        "#15121c"
        if sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722))) > 0.179
        else "#ffffff"
    )


def apply_style(root, colours):
    load_fonts(root)
    c = colours
    root.configure(bg=c["bg"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        ".",
        background=c["card"],
        foreground=c["text"],
        font=(UI_FONT, 11),
        bordercolor=c["border"],
        lightcolor=c["border"],
        darkcolor=c["border"],
        focuscolor=c["accent"],
    )
    for pattern, value in {
        "*TCombobox*Listbox.background": c["card"],
        "*TCombobox*Listbox.foreground": c["text"],
        "*TCombobox*Listbox.selectBackground": c["accent"],
        "*TCombobox*Listbox.selectForeground": contrasting_text(c["accent"]),
        "*TCombobox*Listbox.font": (UI_FONT, 11),
        "*TCombobox*Listbox.relief": "flat",
        "*TCombobox*Listbox.borderWidth": 0,
    }.items():
        root.option_add(pattern, value)
    for name, base, hover, fg in [
        ("TButton", c["card_hover"], c["border"], c["text"]),
        ("Accent.TButton", c["accent"], c["accent_hover"], contrasting_text(c["accent"])),
        ("Nav.TButton", c["sidebar"], c["card_hover"], c["sub_text"]),
        ("Selected.Nav.TButton", c["glow"], c["card_hover"], c["text"]),
        ("Tab.TButton", c["bg"], c["card_hover"], c["sub_text"]),
        ("Selected.Tab.TButton", c["glow"], c["card_hover"], c["text"]),
    ]:
        style.configure(
            name,
            background=base,
            foreground=fg,
            padding=(12, 8) if "Tab" in name else (14, 10),
            borderwidth=0,
            relief="flat",
            focusthickness=0,
            anchor="w" if "Nav" in name else "center",
            font=(
                UI_FONT,
                12 if "Nav" in name else 11,
                "bold" if name.startswith("Selected") else "normal",
            ),
        )
        style.map(
            name,
            background=[
                ("disabled", c["card"]),
                ("pressed", hover),
                ("active", hover),
                ("focus", hover),
            ],
            foreground=[("disabled", c["sub_text"]), ("!disabled", fg)],
            bordercolor=[("focus", c["accent"]), ("!focus", base)],
            lightcolor=[("!disabled", base)],
            darkcolor=[("!disabled", base)],
        )
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(
            name,
            fieldbackground=c["sidebar"],
            background=c["sidebar"],
            foreground=c["text"],
            insertcolor=c["text"],
            arrowcolor=c["sub_text"],
            borderwidth=1,
            padding=9,
            relief="flat",
            selectbackground=c["accent"],
            selectforeground=contrasting_text(c["accent"]),
        )
        style.map(
            name,
            fieldbackground=[
                ("disabled", c["card"]),
                ("readonly", c["sidebar"]),
                ("!disabled", c["sidebar"]),
            ],
            background=[("active", c["card_hover"]), ("!disabled", c["sidebar"])],
            foreground=[("disabled", c["sub_text"]), ("!disabled", c["text"])],
            arrowcolor=[("disabled", c["sub_text"]), ("!disabled", c["text"])],
            bordercolor=[
                ("focus", c["accent"]),
                ("active", c["accent"]),
                ("!disabled", c["border"]),
            ],
            lightcolor=[("!disabled", c["border"])],
            darkcolor=[("!disabled", c["border"])],
        )
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            name,
            background=c["card"],
            foreground=c["text"],
            padding=(0, 6),
            indicatorbackground=c["sidebar"],
            indicatorforeground=c["text"],
            indicatormargin=(0, 0, 10, 0),
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            name,
            background=[("active", c["card"]), ("!disabled", c["card"])],
            foreground=[("disabled", c["sub_text"]), ("!disabled", c["text"])],
            indicatorbackground=[
                ("selected", c["accent"]),
                ("active", c["card_hover"]),
                ("!selected", c["sidebar"]),
            ],
            indicatorforeground=[("selected", contrasting_text(c["accent"]))],
        )
    style.configure(
        "Treeview",
        background=c["card"],
        fieldbackground=c["card"],
        foreground=c["text"],
        rowheight=36,
        borderwidth=0,
        relief="flat",
    )
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.map(
        "Treeview", background=[("selected", c["glow"])], foreground=[("selected", c["text"])]
    )
    style.configure(
        "Treeview.Heading",
        background=c["card_hover"],
        foreground=c["sub_text"],
        padding=(12, 10),
        relief="flat",
        borderwidth=0,
        font=(UI_FONT, 10, "bold"),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", c["border"]), ("!active", c["card_hover"])],
        foreground=[("!disabled", c["text"])],
        relief=[("pressed", "flat")],
    )
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(
            name,
            background=c["border"],
            troughcolor=c["bg"],
            arrowcolor=c["sub_text"],
            borderwidth=0,
            arrowsize=10,
            gripcount=0,
            relief="flat",
        )
        style.map(
            name,
            background=[("active", c["sub_text"]), ("pressed", c["accent"])],
            arrowcolor=[("!disabled", c["sub_text"])],
        )
    style.configure(
        "Horizontal.TProgressbar",
        background=c["accent"],
        troughcolor=c["sidebar"],
        bordercolor=c["sidebar"],
        lightcolor=c["accent"],
        darkcolor=c["accent"],
        thickness=5,
    )
    return style


class RoundedCard(tk.Frame):
    """Native opaque card: one layout pass, no Canvas surface under child widgets."""

    def __init__(self, parent, colours, padding=22):
        super().__init__(
            parent,
            bg=colours["card"],
            highlightthickness=1,
            highlightbackground=colours["border"],
            bd=0,
        )
        self.colours, self.padding = colours, padding
        self.inner = tk.Frame(self, bg=colours["card"])
        self.inner.pack(fill="both", expand=True, padx=padding, pady=padding)
        self._layout_job = self.after_idle(self._initial_layout)
        self.bind("<Destroy>", self._cancel_layout)

    def _initial_layout(self):
        self._layout_job = None

    def _cancel_layout(self, event=None):
        if self._layout_job:
            self.after_cancel(self._layout_job)
            self._layout_job = None


class Disclosure(tk.Frame):
    def __init__(self, parent, title, colours, opened=False):
        super().__init__(parent, bg=parent.cget("bg"))
        self.pack(fill="x", pady=8)
        self.title, self.opened = title, opened
        self.button = ttk.Button(self, command=self.toggle)
        self.button.pack(anchor="w")
        self.body = tk.Frame(self, bg=colours["card"])
        self._draw()

    def _draw(self):
        self.button.configure(text=("−  " if self.opened else "+  ") + self.title)
        if self.opened:
            self.body.pack(fill="x", pady=(12, 0))
        else:
            self.body.pack_forget()

    def toggle(self):
        self.opened = not self.opened
        self._draw()


def colour_mix(a, b, progress):
    channels = [
        round(int(a[i : i + 2], 16) * (1 - progress) + int(b[i : i + 2], 16) * progress)
        for i in (1, 3, 5)
    ]
    return "#" + "".join(f"{v:02x}" for v in channels)


def style_titlebar(window, colours):
    """Keep native drag, snap, keyboard and system menu; tint on supported Windows."""
    import os

    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user = ctypes.WinDLL("user32")
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        hwnd = user.GetParent(window.winfo_id()) or window.winfo_id()
        api = ctypes.WinDLL("dwmapi")
        api.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]

        def put(attribute, value):
            data = ctypes.c_uint(value)
            api.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))

        put(20, 1)
        for attribute, key in ((35, "sidebar"), (36, "text"), (34, "border")):
            hex_value = colours[key].lstrip("#")
            put(attribute, int(hex_value[4:6] + hex_value[2:4] + hex_value[:2], 16))
        put(33, 2)  # Windows 11 rounded window corners.
    except (OSError, AttributeError, tk.TclError):
        pass


class MotionController:
    """Time-bounded interaction animation, zero timers at rest. Respects reduced motion."""

    def __init__(self, root):
        self.root = root
        self.jobs = {}
        self.styles = {}
        root.bind_class("TButton", "<Enter>", self._enter, add="+")
        root.bind_class("TButton", "<Leave>", self._leave, add="+")
        root.bind_class("TButton", "<Destroy>", self._destroy, add="+")

    def enabled(self):
        return (
            not self.root.config_mgr.snapshot().get("accessibility", {}).get("reduce_motion", False)
        )

    def _enter(self, event):
        self._animate(event.widget, True)

    def _leave(self, event):
        self._animate(event.widget, False)

    def _destroy(self, event):
        job = self.jobs.pop(event.widget, None)
        if job:
            self.root.after_cancel(job)
        self.styles.pop(event.widget, None)

    def _animate(self, widget, entering):
        if not isinstance(widget, ttk.Button) or not self.enabled() or widget.instate(["disabled"]):
            return
        previous = self.jobs.pop(widget, None)
        if previous:
            self.root.after_cancel(previous)
        style = ttk.Style(self.root)
        current = str(widget.cget("style")) or "TButton"
        old = self.styles.get(widget)
        base = old[0] if old and current == old[1] else current
        name = f"KrestMotion{widget.winfo_id()}." + base
        self.styles[widget] = (base, name)
        normal = style.lookup(base, "background", ("!active", "!pressed"))
        active = style.lookup(base, "background", ("active",))
        if not normal.startswith("#") or not active.startswith("#"):
            return
        widget.configure(style=name)
        start, end = (normal, active) if entering else (active, normal)

        began = time.monotonic()

        def frame():
            self.jobs.pop(widget, None)
            if not widget.winfo_exists() or str(widget.cget("style")) != name:
                return
            progress = min(1, (time.monotonic() - began) / 0.12)
            colour = colour_mix(start, end, progress)
            style.configure(name, background=colour)
            style.map(
                name,
                background=[
                    ("disabled", self.root.theme["card"]),
                    ("pressed", active),
                    ("!disabled", colour),
                ],
            )
            if progress < 1:
                self.jobs[widget] = self.root.after(24, frame)
            elif not entering:
                widget.configure(style=base)

        frame()


class BrandMark(tk.Canvas):
    def __init__(self, parent, image, colours, motion=lambda: True):
        super().__init__(parent, width=56, height=56, bg=colours["sidebar"], highlightthickness=0)
        self.colours, self.motion, self.job = colours, motion, None
        self.ring = self.create_oval(2, 2, 54, 54, outline=colours["border"], width=1)
        self.create_image(28, 28, image=image)
        self.bind("<Enter>", lambda e: self.pulse())
        self.bind("<Destroy>", self._stop)

    def _stop(self, event=None):
        if self.job:
            self.after_cancel(self.job)
            self.job = None

    def pulse(self, index=0):
        if not self.motion():
            return
        if index == 0:
            self._stop()
        factor = 1 - abs(index - 5) / 5
        self.itemconfigure(
            self.ring,
            outline=colour_mix(self.colours["border"], self.colours["accent"], factor),
            width=1 + factor,
        )
        self.job = self.after(35, self.pulse, index + 1) if index < 10 else None
