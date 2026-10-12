"""Accessible themed app dialogs; file pickers remain native Windows dialogs."""

from ui_fonts import UI_FONT

import tkinter as tk
from tkinter import ttk
from ui_design import style_titlebar
from app_icons import install_window_icon
from themes import THEMES


def _dialog(title, text, choices, parent=None, entry=False, initialvalue="", show=None):
    parent = parent or tk._default_root
    colours = getattr(parent, "theme", THEMES["Graphite"])
    window = tk.Toplevel(parent)
    window.title(title)
    window.configure(bg=colours["bg"])
    window.resizable(False, False)
    window.transient(parent)
    window._icons = install_window_icon(window)
    window.result = None
    frame = tk.Frame(window, bg=colours["bg"], padx=28, pady=26)
    frame.pack(fill="both", expand=True)
    tk.Label(
        frame,
        text=title,
        bg=colours["bg"],
        fg=colours["text"],
        font=(UI_FONT, 17, "bold"),
        anchor="w",
    ).pack(fill="x", pady=(0, 14))
    tk.Label(
        frame,
        text=text,
        bg=colours["bg"],
        fg=colours["sub_text"],
        font=(UI_FONT, 11),
        wraplength=500,
        justify="left",
        anchor="w",
    ).pack(fill="x", pady=(0, 20))
    field = None
    if entry:
        field = ttk.Entry(frame, width=50, show=show or "")
        field.insert(0, str(initialvalue or ""))
        field.pack(fill="x", pady=(0, 18))
    buttons = tk.Frame(frame, bg=colours["bg"])
    buttons.pack(fill="x")

    def finish(value):
        window.result = field.get() if entry and value is True else value
        window.destroy()

    first = None
    for label, result, primary in choices:
        button = ttk.Button(
            buttons,
            text=label,
            style="Accent.TButton" if primary else "TButton",
            command=lambda v=result: finish(v),
        )
        button.pack(side="left", padx=(0, 8))
        if first is None:
            first = button
    window.bind("<Escape>", lambda e: finish(None))
    window.bind("<Return>", lambda e: finish(choices[0][1]))
    window.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    window.update_idletasks()
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    x = max(0, parent.winfo_rootx() + (parent.winfo_width() - width) // 2)
    y = max(0, parent.winfo_rooty() + (parent.winfo_height() - height) // 2)
    window.geometry(f"+{x}+{y}")
    style_titlebar(window, colours)
    window.grab_set()
    (field or first).focus_set()
    parent.wait_window(window)
    return window.result


class MessageBox:
    def showinfo(self, title, message, parent=None, **kwargs):
        return _dialog(title, message, [("Понятно", "ok", True)], parent)

    showerror = showinfo
    showwarning = showinfo

    def askyesno(self, title, message, parent=None, **kwargs):
        return _dialog(title, message, [("Да", True, True), ("Нет", False, False)], parent) is True

    def askyesnocancel(self, title, message, parent=None, **kwargs):
        return _dialog(
            title,
            message,
            [("Сохранить", True, True), ("Не сохранять", False, False), ("Отмена", None, False)],
            parent,
        )

    def askokcancel(self, title, message, parent=None, **kwargs):
        return (
            _dialog(title, message, [("Продолжить", True, True), ("Отмена", False, False)], parent)
            is True
        )


class SimpleDialog:
    def askstring(self, title, prompt, parent=None, initialvalue="", **kwargs):
        return _dialog(
            title,
            prompt,
            [("Готово", True, True), ("Отмена", None, False)],
            parent,
            entry=True,
            initialvalue=initialvalue,
            show=kwargs.get("show"),
        )

    def askinteger(self, title, prompt, parent=None, minvalue=None, maxvalue=None, **kwargs):
        while True:
            value = self.askstring(title, prompt, parent, **kwargs)
            if value is None:
                return None
            try:
                number = int(value)
                if (minvalue is None or number >= minvalue) and (
                    maxvalue is None or number <= maxvalue
                ):
                    return number
            except ValueError:
                pass
            messagebox.showerror(
                title, f"Введите целое число в диапазоне {minvalue}–{maxvalue}.", parent=parent
            )


messagebox = MessageBox()
simpledialog = SimpleDialog()
