"""One-click source launcher and component installer; needs only Python + Tk."""

import argparse
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from dependency_setup import (
    ROOT,
    inspect,
    install_commands,
    run_command,
    console_python,
    choose_environment,
)
from process_lock import ProcessLock
from themes import THEMES
from ui_design import apply_style, style_titlebar, Disclosure
from ui_fonts import UI_FONT, load_fonts


class SetupWindow(tk.Tk):
    def __init__(self, profile="all", executable=None, launch=True):
        super().__init__()
        self.profile, self.launch = profile, launch
        self.choose_environment = profile == "all" and executable is None
        self.executable = (
            Path(executable)
            if executable
            else ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.ready = False
        self.report = None
        self.title("Крест · Подключение компонентов")
        if os.name == "nt":
            self.iconbitmap(str(ROOT / "assets" / "krest.ico"))
        self.geometry("620x440")
        self.minsize(560, 420)
        load_fonts(self)
        self.theme = THEMES["Amethyst Modern"]
        apply_style(self, self.theme)
        style_titlebar(self, self.theme)
        frame = tk.Frame(self, bg=self.theme["bg"], padx=28, pady=24)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="Всё подготовим сами",
            bg=self.theme["bg"],
            fg=self.theme["text"],
            font=(UI_FONT, 23, "bold"),
        ).pack(anchor="w")
        self.status = tk.StringVar(value="Проверяю компоненты…")
        tk.Label(
            frame,
            textvariable=self.status,
            bg=self.theme["bg"],
            fg=self.theme["sub_text"],
            font=(UI_FONT, 11),
            wraplength=550,
            justify="left",
        ).pack(fill="x", pady=16)
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=6)
        self.button = ttk.Button(
            frame, text="Проверяю…", command=self.install, style="Accent.TButton", state="disabled"
        )
        self.button.pack(anchor="w", pady=12)
        self.log_panel = Disclosure(frame, "Подробности подключения", self.theme)
        self.details = tk.Text(
            self.log_panel.body,
            height=6,
            bg=self.theme["card"],
            fg=self.theme["sub_text"],
            relief="flat",
            font=(UI_FONT, 9),
            wrap="word",
            state="disabled",
        )
        self.details.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_after = self.after(100, self.poll)
        self.check()

    def background(self, work):
        self.busy = True
        self.cancel.clear()
        self.progress.start(16)
        self.button.configure(state="disabled")

        events = self.events

        def worker():
            try:
                work()
            except Exception as exc:
                events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def check(self):
        # Workers own plain data only; closing the window must not transfer the
        # last reference to a Tk variable/interpreter to a background thread.
        choose, executable, profile = self.choose_environment, self.executable, self.profile
        events = self.events

        def work():
            selected = choose_environment(ROOT) if choose else executable
            report = inspect(selected, profile) if selected.is_file() else None
            events.put(("checked", (report, selected)))

        self.background(work)

    def install(self):
        if self.ready:
            self.finish()
            return
        self.status.set(
            "Подключаю компоненты. Можно оставить это окно открытым — команды вводить не нужно."
        )

        executable, profile = self.executable, self.profile
        events, cancel = self.events, self.cancel

        def work():
            lock = ProcessLock(ROOT / ".setup.lock")
            if not lock.acquire():
                raise RuntimeError(
                    "Установка уже идёт в другом окне Крест. Дождитесь её завершения."
                )
            try:
                emit = lambda line: events.put(("log", line))
                if not executable.is_file():
                    # Never install into a guessed or system interpreter for a repair request.
                    if profile != "all":
                        raise RuntimeError(
                            "Среда запущенной Крест недоступна. Закройте Крест и откройте start.bat."
                        )
                    run_command(
                        [
                            str(console_python(sys.executable)),
                            "-m",
                            "venv",
                            str(executable.parent.parent),
                        ],
                        emit,
                        cancel,
                    )
                run_command(
                    [str(console_python(executable)), "-m", "ensurepip", "--upgrade"],
                    emit,
                    cancel,
                )
                report = inspect(executable, profile)
                for command in install_commands(executable, profile, report):
                    run_command(command, emit, cancel)
                verified = inspect(executable, profile)
                if verified["missing"] or verified["broken"]:
                    raise RuntimeError(
                        "Часть компонентов не подключилась. Нажмите кнопку ещё раз.\n"
                        + ", ".join(verified["missing"] + verified["broken"])
                    )
                events.put(("installed", None))
            finally:
                lock.close()

        self.background(work)

    def poll(self):
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.details.configure(state="normal")
                self.details.insert("end", value + "\n")
                if int(self.details.index("end-1c").split(".")[0]) > 200:
                    self.details.delete("1.0", "50.0")
                self.details.see("end")
                self.details.configure(state="disabled")
                continue
            if self.cancel.is_set():
                self.destroy()
                return
            self.busy = False
            self.progress.stop()
            self.button.configure(state="normal")
            if kind == "error":
                self.status.set(value.splitlines()[0][:280])
                self.details.configure(state="normal")
                self.details.insert("end", value + "\n")
                self.details.configure(state="disabled")
                if not self.log_panel.opened:
                    self.log_panel.toggle()
                self.button.configure(text="Повторить подключение")
            elif kind == "checked":
                value, self.executable = value
                self.report = value
                self.ready = value is not None and not value["missing"] and not value["broken"]
                if self.ready:
                    if self.launch:
                        self.finish()
                        return
                    self.status.set(
                        "Компоненты музыки уже подключены. Вернитесь в Музыку и нажмите «Обновить трек»."
                    )
                    self.button.configure(text="Готово")
                    continue
                self.status.set(
                    "Для музыки, голоса и работы с Windows нужны дополнительные компоненты. Скачаю их из официальных каталогов Python. Нужен интернет; первая установка голоса может занять несколько минут."
                    if self.profile == "all"
                    else "Подключу управление плеерами, громкостью и кнопками «Нравится». Нужен интернет. Ваши настройки и модели сохранятся."
                )
                self.button.configure(
                    text="Подготовить и открыть Крест" if self.launch else "Подключить музыку"
                )
            elif kind == "installed":
                self.ready = True
                self.status.set(
                    "Готово. Компоненты подключены. Вернитесь в Музыку и нажмите «Обновить трек»."
                    if not self.launch
                    else "Всё готово. Можно открыть Крест."
                )
                self.button.configure(text="Открыть Крест" if self.launch else "Готово")
                if self.launch:
                    self.finish()
                    return
        self._poll_after = self.after(100, self.poll)

    def destroy(self):
        self.cancel.set()
        if self._poll_after is not None:
            self.after_cancel(self._poll_after)
            self._poll_after = None
        self.progress.stop()
        # Variable.__del__ calls Tcl. Break the root → variable → root cycle
        # while we are still on the GUI thread, before a worker can trigger GC.
        self.status = None
        descendants = []
        pending = list(self.children.values())
        while pending:
            widget = pending.pop()
            descendants.append(widget)
            pending.extend(widget.children.values())
        super().destroy()
        # Composite widgets keep Python attributes pointing to their children.
        # Tk.destroy clears children dictionaries, but not child.master: break
        # those remaining cycles so the Tcl interpreter cannot be GC'd later
        # by the install worker (Tcl_AsyncDelete otherwise aborts the process).
        for widget in descendants:
            widget.master = None

    def finish(self):
        if self.launch:
            python = self.executable
            windowless = python.with_name("pythonw.exe")
            if os.name == "nt" and windowless.exists():
                python = windowless
            try:
                subprocess.Popen(
                    [str(python), str(ROOT / "main.py")],
                    cwd=ROOT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                self.status.set("Не удалось открыть Крест: " + str(exc))
                self.button.configure(text="Открыть Крест", state="normal")
                return
        self.destroy()

    def close(self):
        if self.busy:
            if messagebox.askyesno(
                "Прервать подготовку?",
                "Подготовку можно будет повторить при следующем запуске.",
                parent=self,
            ):
                self.cancel.set()
                self.status.set("Останавливаю подготовку…")
            return
        self.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--music", action="store_true")
    parser.add_argument("--python")
    args = parser.parse_args()
    SetupWindow("music" if args.music else "all", args.python, launch=not args.music).mainloop()
