"""Read-only command inspection using the same normalization and dispatcher as speech."""

from speech_tools import normalize_command
from speech_recognition import decode_voice_command
from command_understanding import exact_command, suggestions


def parse_command(manager, text, settings):
    text = str(text).strip()
    if len(text) > 2000:
        raise ValueError("Максимум 2000 символов")
    wake, command = decode_voice_command(manager, text, settings)
    normalized = exact_command(manager, command) or normalize_command(
        command, settings.get("command_aliases", {})
    )
    match = manager.match(normalized)
    entry, params = match if match else (None, {})
    return {
        "input": text,
        "wake": wake,
        "normalized": normalized,
        "skill": entry.id if entry else "",
        "description": entry.description if entry else "",
        "availability": manager.availability(entry) if entry else "Команда не найдена",
        "parameters": params,
        "suggestions": [row["command"] for row in suggestions(manager, normalized)]
        if not entry
        else [],
    }


def open_parser(app):
    import tkinter as tk
    from tkinter import ttk
    from ui_design import style_titlebar
    from app_icons import install_window_icon
    from ui_fonts import UI_FONT

    existing = getattr(app, "_parser_window", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    win = tk.Toplevel(app)
    app._parser_window = win
    win.title("Крест · Разбор команды")
    win.geometry("660x450")
    win.configure(bg=app.theme["bg"])
    win.transient(app)
    win._icons = install_window_icon(win)
    box = tk.Frame(win, bg=app.theme["bg"], padx=24, pady=24)
    box.pack(fill="both", expand=True)
    app.label(box, "Как Крест понимает фразу", large=True).pack(anchor="w")
    app.label(
        box,
        "Проверка ничего не выполняет. Можно вставить фразу из истории распознавания.",
        muted=True,
        wraplength=600,
    ).pack(anchor="w", pady=12)
    phrase = tk.StringVar(value="Крест, сделай скриншот")
    entry = ttk.Entry(box, textvariable=phrase)
    entry.pack(fill="x")
    result = tk.StringVar(value="Введите фразу и нажмите «Разобрать».")

    def done(data):
        if not win.winfo_exists():
            return
        button.configure(state="normal")
        if isinstance(data, str):
            result.set(data)
            return
        params = "; ".join(f"{key}: {value}" for key, value in data["parameters"].items()) or "Нет"
        result.set(
            f"После обработки: {data['normalized']}\nОбращение: {'распознано' if data['wake'] else 'не указано'}\nНавык: {data['description'] or 'не найден'}\nСостояние: {data['availability']}\nПараметры: {params}"
            + (
                "\nПохожие команды: " + "; ".join(data["suggestions"])
                if data["suggestions"]
                else ""
            )
        )

    def run():
        text = phrase.get()
        button.configure(state="disabled")

        def work():
            try:
                if hasattr(app.runtime, "client"):
                    return app.runtime.client.call("command.parse", text=text)
                return parse_command(app.runtime.manager, text, app.config_mgr.snapshot())
            except Exception as exc:
                return "Ошибка проверки: " + str(exc)

        app._background(work, done)

    button = ttk.Button(box, text="Разобрать", style="Accent.TButton", command=run)
    button.pack(anchor="w", pady=12)
    tk.Label(
        box,
        textvariable=result,
        bg=app.theme["bg"],
        fg=app.theme["text"],
        font=(UI_FONT, 11),
        wraplength=600,
        justify="left",
        anchor="nw",
    ).pack(fill="both", expand=True)
    win.bind("<Return>", lambda event: run())
    win.bind("<Escape>", lambda event: win.destroy())
    style_titlebar(win, app.theme)
    entry.focus_set()
