"""Skill import and management inside the existing themed desktop."""

from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from ui_dialogs import messagebox
from ui_design import style_titlebar
from app_icons import install_window_icon


class SkillImportPages:
    def build_skill_imports(self):
        card = self.card(
            self.page_bodies["skills"],
            "Добавить свои навыки",
            "Выберите Python-файл. Крест найдёт команды и подключит их без изменения main.py и перезапуска.",
        )
        toolbar = tk.Frame(card, bg=self.theme["card"])
        toolbar.pack(fill="x", pady=(5, 10))
        for title, action in [
            ("Импортировать .py", self._choose_skill_import),
            ("Скачать шаблон", self._save_skill_template),
            ("Обновить список", self._refresh_skill_imports),
        ]:
            ttk.Button(toolbar, text=title, command=action).pack(side="left", padx=(0, 8))
        self.import_tree = ttk.Treeview(
            card, columns=("name", "count", "status"), show="headings", height=3
        )
        for key, label, width in [
            ("name", "Модуль", 260),
            ("count", "Команд", 90),
            ("status", "Состояние", 300),
        ]:
            self.import_tree.heading(key, text=label)
            self.import_tree.column(key, width=width)
        self.import_tree.pack(fill="x")
        controls = tk.Frame(card, bg=self.theme["card"])
        controls.pack(fill="x", pady=(10, 5))
        for title, action in [
            ("Включить", "enable"),
            ("Отключить", "disable"),
            ("Удалить", "remove"),
        ]:
            ttk.Button(
                controls, text=title, command=lambda a=action: self._change_skill_import(a)
            ).pack(side="left", padx=(0, 8))
        self.import_status = tk.StringVar(
            value="Подключённые навыки появятся в общем каталоге и поиске команд."
        )
        self.label(card, textvariable=self.import_status, muted=True, wraplength=750).pack(
            fill="x", pady=5
        )
        self._refresh_skill_imports()

    def _refresh_skill_imports(self):
        def done(data):
            if not hasattr(self, "import_tree") or not self.import_tree.winfo_exists():
                return
            selected = self.import_tree.selection()
            self.import_tree.delete(*self.import_tree.get_children())
            for row in data["modules"]:
                status = row["error"] or ("Включён" if row["enabled"] else "Отключён")
                self.import_tree.insert(
                    "", "end", iid=row["id"], values=(row["name"], row["count"], status)
                )
            if selected and self.import_tree.exists(selected[0]):
                self.import_tree.selection_set(selected[0])
            if data["error"]:
                self.import_status.set(data["error"])

        self._background(lambda: self.runtime.skill_import("list"), done=done)

    def _save_skill_template(self):
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить шаблон навыка",
            initialfile="my_skill.py",
            defaultextension=".py",
            filetypes=[("Python", "*.py")],
        )
        if path:
            try:
                from skill_importer import TEMPLATE

                Path(path).write_text(TEMPLATE, encoding="utf-8")
                self.import_status.set(
                    "Шаблон сохранён. Его можно сразу импортировать и проверить командой «моя новая команда»."
                )
            except OSError as exc:
                self._report_error(str(exc))

    def _choose_skill_import(self):
        path = filedialog.askopenfilename(
            parent=self, title="Выберите файл навыка", filetypes=[("Python", "*.py")]
        )
        if path:
            self.import_status.set("Проверяю файл без запуска его кода…")

            def work():
                try:
                    preview = self.runtime.skill_import("preview", path=path)
                    self.post_ui(lambda: self._show_skill_preview(path, preview))
                except Exception as exc:
                    from integrations import redact

                    error = redact(str(exc), self.config_mgr.snapshot())

                    def report():
                        self.import_status.set(error)
                        self._report_error(error)

                    self.post_ui(report)

            self._background(work)

    def _show_skill_preview(self, path, preview):
        old = getattr(self, "_skill_import_dialog", None)
        if old and old.winfo_exists():
            old.destroy()
        win = tk.Toplevel(self)
        self._skill_import_dialog = win
        win.title("Импорт навыка · " + preview["name"])
        win.configure(bg=self.theme["bg"])
        win.geometry("860x620")
        win.minsize(700, 560)
        win.transient(self)
        win._icons = install_window_icon(win)
        body = tk.Frame(win, bg=self.theme["bg"], padx=24, pady=20)
        body.pack(fill="both", expand=True)
        self.label(body, "Подключить новые команды", large=True).pack(anchor="w")
        self.label(
            body, preview["name"] + " · Найдено функций: " + str(len(preview["skills"])), muted=True
        ).pack(anchor="w", pady=(8, 12))
        tree = ttk.Treeview(
            body, columns=("function", "commands", "description"), show="headings", height=7
        )
        for key, title, width in [
            ("function", "Функция", 160),
            ("commands", "Фразы / шаблоны", 280),
            ("description", "Описание", 260),
        ]:
            tree.heading(key, text=title)
            tree.column(key, width=width)
        tree.pack(fill="both", expand=True)
        bindings = {}
        for i, row in enumerate(preview["skills"]):
            tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    row["function"],
                    "; ".join(row["keywords"] or row["patterns"]),
                    row["description"],
                ),
            )
            if not row["decorated"]:
                bindings[row["function"]] = list(row["keywords"])
        self.label(
            body,
            "Для обычной функции выберите строку и задайте фразы через точку с запятой.",
            muted=True,
            wraplength=760,
        ).pack(anchor="w", pady=(12, 5))
        command = tk.StringVar()
        editor = ttk.Entry(body, textvariable=command)
        editor.pack(fill="x")
        selected_plain = [None]

        def select(event=None):
            selected_plain[0] = None
            if not tree.selection():
                return
            row = preview["skills"][int(tree.selection()[0])]
            command.set("; ".join(bindings.get(row["function"], row["keywords"])))
            editor.configure(state="disabled" if row["decorated"] else "normal")
            selected_plain[0] = row["function"] if not row["decorated"] else None

        def edit(*args):
            name = selected_plain[0]
            if name:
                bindings[name] = [v.strip() for v in command.get().split(";") if v.strip()]
                iid = tree.selection()[0]
                tree.set(iid, "commands", "; ".join(bindings[name]))

        command.trace_add("write", edit)
        tree.bind("<<TreeviewSelect>>", select)
        if tree.get_children():
            tree.selection_set("0")
            select()
        note = "Файл с таким же именем обновит установленный модуль. Фразы @skill берутся из файла автоматически.\nPython-код выполняется с вашими правами: подключайте только знакомые вам файлы."
        if preview["warnings"]:
            note += "\n" + "\n".join(preview["warnings"][:3])
        self.label(body, note, muted=True, wraplength=770).pack(anchor="w", pady=(12, 8))
        result_var = tk.StringVar(
            value="Предпросмотр не запускает код и не устанавливает зависимости."
        )
        self.label(body, textvariable=result_var, wraplength=760).pack(fill="x", pady=(0, 10))
        buttons = tk.Frame(body, bg=self.theme["bg"])
        buttons.pack(fill="x")

        def install():
            if any(not v for v in bindings.values()):
                result_var.set("У каждой обычной функции должна быть хотя бы одна фраза.")
                return
            payload = {key: list(values) for key, values in bindings.items()}
            install_button.configure(state="disabled")
            result_var.set("Подключаю модуль…")

            def finish(result, error=None):
                if error:
                    self._report_error(error)
                    if win.winfo_exists():
                        result_var.set(error)
                        install_button.configure(state="normal")
                    return
                self.import_status.set(f"Подключён {result['name']}: команд — {result['count']}.")
                self.refresh_skills()
                self._refresh_skill_imports()
                if win.winfo_exists():
                    win.destroy()

            def work():
                try:
                    result = self.runtime.skill_import(
                        "install", path=path, sha256=preview["sha256"], bindings=payload
                    )
                    self.post_ui(lambda: finish(result))
                except Exception as exc:
                    from integrations import redact

                    error = redact(str(exc), self.config_mgr.snapshot())
                    self.post_ui(lambda error=error: finish(None, error))

            self._background(work)

        install_button = ttk.Button(
            buttons, text="Подключить команды", style="Accent.TButton", command=install
        )
        install_button.pack(side="left")
        ttk.Button(buttons, text="Отмена", command=win.destroy).pack(side="left", padx=10)
        win.bind("<Escape>", lambda event: win.destroy())
        win.update_idletasks()
        style_titlebar(win, self.theme)
        win.grab_set()

    def _change_skill_import(self, action):
        selected = self.import_tree.selection()
        if not selected:
            self.import_status.set("Выберите импортированный модуль в списке.")
            return
        ident = selected[0]
        if action == "remove" and not messagebox.askyesno(
            "Удаление навыка",
            "Удалить модуль из Крест? Исходный файл, который вы выбирали, сохранится.",
            parent=self,
        ):
            return

        def done(_):
            self.import_status.set(
                {
                    "enable": "Модуль включён.",
                    "disable": "Модуль отключён.",
                    "remove": "Модуль удалён.",
                }[action]
            )
            self.refresh_skills()
            self._refresh_skill_imports()

        self._background(lambda: self.runtime.skill_import(action, id=ident), done=done)
