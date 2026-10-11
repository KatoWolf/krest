"""Clipboard controls for editable fields, including Windows Cyrillic shortcuts."""

import os
import tkinter as tk


def paste(entry, replace=False):
    if str(entry.cget("state")) in ("disabled", "readonly"):
        return "break"
    try:
        value = entry.clipboard_get().replace("\r", "").replace("\n", " ")
    except tk.TclError:
        return "break"
    if not value:
        return "break"
    if replace:
        entry.delete(0, "end")
    elif entry.selection_present():
        entry.delete("sel.first", "sel.last")
    entry.insert("insert", value)
    entry.focus_set()
    return "break"


def shortcut(entry, event):
    keys = {
        "v": "paste",
        "cyrillic_em": "paste",
        "a": "all",
        "cyrillic_ef": "all",
        "c": "copy",
        "cyrillic_es": "copy",
        "x": "cut",
        "cyrillic_che": "cut",
    }
    action = keys.get(event.keysym.casefold())
    if os.name == "nt":
        action = {86: "paste", 65: "all", 67: "copy", 88: "cut"}.get(event.keycode, action)
    if action == "paste":
        return paste(entry)
    if action == "all":
        entry.selection_range(0, "end")
        entry.icursor("end")
    elif action in ("copy", "cut"):
        entry.event_generate("<<Copy>>" if action == "copy" else "<<Cut>>")
    else:
        return None
    return "break"


def attach(entry, theme):
    entry.bind("<<Paste>>", lambda _: paste(entry))
    entry.bind("<Shift-Insert>", lambda _: paste(entry))
    entry.bind("<Control-KeyPress>", lambda event: shortcut(entry, event))
    menu = tk.Menu(
        entry,
        tearoff=False,
        bg=theme["card"],
        fg=theme["text"],
        activebackground=theme["accent"],
        activeforeground=theme["text"],
    )
    menu.add_command(label="Вставить", command=lambda: paste(entry))
    menu.add_command(label="Заменить текст из буфера", command=lambda: paste(entry, replace=True))
    menu.add_separator()
    menu.add_command(label="Копировать", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Выделить всё", command=lambda: entry.selection_range(0, "end"))

    def popup(event):
        entry.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    entry.bind("<Button-3>", popup)
