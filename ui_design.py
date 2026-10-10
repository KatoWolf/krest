"""Shared visual system. Explicit state colours avoid native white hover surfaces."""

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
    c = colours
    root.configure(bg=c["bg"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        ".",
        background=c["card"],
        foreground=c["text"],
        font=("Segoe UI", 10),
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
        "*TCombobox*Listbox.font": ("Segoe UI", 11),
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
        ("Selected.Tab.TButton", c["card_hover"], c["border"], c["text"]),
    ]:
        style.configure(
            name,
            background=base,
            foreground=fg,
            padding=(14, 10),
            borderwidth=0,
            relief="flat",
            focusthickness=0,
            anchor="w" if "Nav" in name else "center",
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
        font=("Segoe UI", 9, "bold"),
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


class RoundedCard(tk.Canvas):
    def __init__(self, parent, colours, padding=22):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0, height=1)
        self.colours, self.padding = colours, padding
        self.inner = tk.Frame(self, bg=colours["card"])
        self.window = self.create_window(padding, padding, anchor="nw", window=self.inner)
        self.bind("<Configure>", self._resize)
        self.inner.bind("<Configure>", self._content)
        self.after_idle(self._content)

    def _content(self, event=None):
        height = self.inner.winfo_reqheight() + 2 * self.padding
        if int(self.cget("height")) != height:
            self.configure(height=height)

    def _resize(self, event=None):
        width, height = self.winfo_width(), self.winfo_height()
        self.itemconfigure(self.window, width=max(1, width - 2 * self.padding))
        self._content()
        self.delete("surface")
        r = 18
        self.create_polygon(
            r,
            1,
            width - r,
            1,
            width - 1,
            1,
            width - 1,
            r,
            width - 1,
            height - r,
            width - 1,
            height - 1,
            width - r,
            height - 1,
            r,
            height - 1,
            1,
            height - 1,
            1,
            height - r,
            1,
            r,
            1,
            1,
            smooth=True,
            splinesteps=24,
            fill=self.colours["card"],
            outline=self.colours["border"],
            tags="surface",
        )
        self.tag_lower("surface")


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
