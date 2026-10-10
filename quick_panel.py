"""Search navigation and commands from a keyboard-friendly floating panel."""

import tkinter as tk
from tkinter import ttk
from ui_design import style_titlebar
from app_icons import install_window_icon


def show_panel(app):
    existing = getattr(app, "_quick_panel", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        existing.entry.focus_set()
        return
    window = tk.Toplevel(app)
    app._quick_panel = window
    window.title("Крест · быстрый доступ")
    window.configure(bg=app.theme["bg"])
    window.geometry("680x490")
    window.minsize(520, 380)
    window._icons = install_window_icon(window)
    window.attributes("-topmost", True)
    frame = tk.Frame(window, bg=app.theme["bg"], padx=22, pady=22)
    frame.pack(fill="both", expand=True)
    app.label(frame, "Что сделать?", large=True).pack(anchor="w", pady=(0, 16))
    variable = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=variable, font=("Segoe UI", 15))
    window.entry = entry
    entry.pack(fill="x", pady=(0, 12))
    tree = ttk.Treeview(frame, columns=("title", "kind"), show="headings", height=6)
    tree.heading("title", text="Команда или страница")
    tree.heading("kind", text="Раздел")
    tree.column("title", width=420)
    tree.column("kind", width=120)
    tree.pack(fill="both", expand=True)
    index = []
    pages = [("page", code, title, "Страница") for code, title in app.NAV]

    def search(*args):
        skills = [
            ("command", example, example, item.category)
            for item in app.runtime.manager.catalog()
            for example in (item.examples or item.keywords)[:2]
        ]
        query = variable.get().casefold().strip()
        index[:] = [row for row in [*pages, *skills] if not query or query in row[2].casefold()][
            :60
        ]
        tree.delete(*tree.get_children())
        for i, row in enumerate(index):
            tree.insert("", "end", iid=str(i), values=(row[2], row[3]))

    def run(event=None):
        selected = tree.selection()
        if selected:
            kind, value, _, _ = index[int(selected[0])]
            if kind == "page":
                app.restore_window()
                app.switch_page(value)
            else:
                app.runtime.submit(value)
        elif variable.get().strip():
            app.runtime.submit(variable.get())
        else:
            return "break"
        window.destroy()
        return "break"

    def choose(event=None):
        rows = tree.get_children()
        if rows:
            tree.selection_set(rows[0])
            tree.focus_set()
        return "break"

    variable.trace_add("write", search)
    entry.bind("<Down>", choose)
    entry.bind("<Return>", run)
    tree.bind("<Return>", run)
    tree.bind("<Double-1>", run)
    window.bind("<Escape>", lambda e: window.destroy())
    app.label(frame, "Enter — выполнить · ↓ — выбрать · Esc — закрыть", muted=True).pack(
        anchor="w", pady=(12, 0)
    )
    search()
    window.update_idletasks()
    window.geometry(
        f"+{max(0, (window.winfo_screenwidth() - 680) // 2)}+{max(0, (window.winfo_screenheight() - 490) // 3)}"
    )
    style_titlebar(window, app.theme)
    entry.focus_force()
