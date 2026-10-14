"""Bounded local file metadata index and explicit short-lived file context."""

import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager

GROUPS = {
    "архив": {".zip", ".7z", ".rar"},
    "документ": {".pdf", ".doc", ".docx", ".odt", ".txt", ".xlsx"},
    "установщик": {".exe", ".msi", ".msix"},
    "скриншот": {".png", ".jpg", ".jpeg"},
}


class DesktopContext:
    def __init__(self, settings, path):
        self.settings, self.path = settings, str(path)
        self.lock = threading.RLock()
        self.folder = self.file = None
        self.selected_at = 0
        self.choices = []
        self.indexing = False
        self.last_index = 0
        self.index_error = ""
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, name TEXT, ext TEXT, modified REAL, size INTEGER, folder TEXT)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS files_modified ON files(modified DESC)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def roots(self):
        cfg = self.settings()
        paths = [
            Path(cfg["app_settings"]["downloads_path"]),
            Path.home() / "Documents",
            Path.home() / "Desktop",
        ]
        paths += [Path(p) for p in cfg.get("file_policy", {}).get("folders", [])]
        return list(
            dict.fromkeys(
                p.expanduser().resolve() for p in paths if p.is_dir() and not p.is_symlink()
            )
        )

    def scan(self, stop=None):
        with self.lock:
            if self.indexing:
                return
            self.indexing = True
        try:
            rows = []
            deadline = time.monotonic() + 15
            limited = False
            for root in self.roots():
                for parent, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = [
                        n
                        for n in dirs
                        if not n.startswith(".")
                        and n.casefold()
                        not in {"node_modules", "__pycache__", "venv", "windows", "appdata"}
                        and not (Path(parent) / n).is_symlink()
                    ]
                    if len(Path(parent).relative_to(root).parts) >= 6:
                        dirs.clear()
                    for name in files:
                        if (
                            time.monotonic() > deadline
                            or len(rows) >= 15000
                            or (stop and stop.is_set())
                        ):
                            limited = True
                            break
                        path = Path(parent) / name
                        if name.startswith(".") or path.is_symlink():
                            continue
                        try:
                            info = path.stat()
                            if path.is_file():
                                rows.append(
                                    (
                                        str(path),
                                        name.casefold(),
                                        path.suffix.casefold(),
                                        info.st_mtime,
                                        info.st_size,
                                        str(root),
                                    )
                                )
                        except OSError:
                            continue
                    if limited:
                        break
                if limited:
                    break
            if stop and stop.is_set():
                return
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM files")
                db.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)", rows)
            self.last_index = time.time()
            self.index_error = "Индекс ограничен 15 000 файлами / 15 секундами." if limited else ""
        except (OSError, sqlite3.Error) as exc:
            self.index_error = str(exc)
        finally:
            self.indexing = False

    def _fresh(self):
        if time.monotonic() - self.selected_at > 300:
            self.folder = self.file = None
            self.choices = []

    def set_folder(self, name, open_window=True):
        cfg = self.settings()
        folders = {
            "загрузки": Path(cfg["app_settings"]["downloads_path"]),
            "документы": Path.home() / "Documents",
            "рабочий стол": Path.home() / "Desktop",
        }
        path = folders.get(name.casefold())
        if path is None or not path.is_dir():
            raise ValueError("Папка не найдена. Проверьте путь в разделе файлов.")
        with self.lock:
            self.folder, self.file = path.resolve(), None
            self.selected_at = time.monotonic()
        if open_window:
            if os.name != "nt":
                raise ValueError("Открытие папок доступно в Windows")
            os.startfile(str(path))
        return "Выбрана папка: " + str(path)

    def search(self, query):
        with self.lock:
            self._fresh()
            value = query.casefold().strip()
            extensions = next((v for k, v in GROUPS.items() if k in value), set())
            latest = any(w in value for w in ("последний", "последнюю", "свежий", "недавний"))
            days = (
                1
                if "сегодня" in value
                else 2
                if "вчера" in value
                else 7
                if "неделю" in value
                else None
            )
            words = re.sub(
                r"\b(?:последний|последнюю|свежий|недавний|файл|файлы|архив|документ|установщик|скриншот|сегодня|вчера|за|неделю|скачанный|измененный|изменённый)\b",
                "",
                value,
            ).strip(' «»"')
            clauses, args = [], []
            if extensions:
                clauses.append("ext IN (" + ",".join("?" for _ in extensions) + ")")
                args.extend(sorted(extensions))
            for word in words.split():
                clauses.append("name LIKE ? ESCAPE '\\'")
                args.append(
                    "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
            if days:
                clauses.append("modified>=?")
                args.append(time.time() - days * 86400)
            if self.folder:
                clauses.append("substr(path,1,?)=?")
                prefix = str(self.folder) + os.sep
                args.extend((len(prefix), prefix))
            with self.connect() as db:
                rows = db.execute(
                    "SELECT path FROM files"
                    + (" WHERE " + " AND ".join(clauses) if clauses else "")
                    + " ORDER BY modified DESC LIMIT 30",
                    args,
                ).fetchall()
            roots = self.roots()
            self.choices = [
                Path(r[0])
                for r in rows
                if Path(r[0]).is_file()
                and not Path(r[0]).is_symlink()
                and any(Path(r[0]).resolve().is_relative_to(root) for root in roots)
            ][:5]
            self.file = (
                self.choices[0] if self.choices and (latest or len(self.choices) == 1) else None
            )
            self.selected_at = time.monotonic()
            if not self.choices:
                return "Файлы не найдены." + (
                    " Индекс ещё обновляется." if self.indexing or not self.last_index else ""
                )
            if self.file:
                return (
                    "Выбран файл: "
                    + str(self.file)
                    + ". Можно сказать «открой выбранный файл» или «покажи его в папке»."
                )
            return "Найдено несколько файлов. Скажите «выбери файл» и номер: " + "; ".join(
                f"{i}. {p.name}" for i, p in enumerate(self.choices, 1)
            )

    def choose(self, number):
        with self.lock:
            self._fresh()
            if not 1 <= number <= len(self.choices):
                raise ValueError("Сначала найдите файл и выберите номер из результатов.")
            self.file = self.choices[number - 1]
            self.selected_at = time.monotonic()
            return "Выбран файл: " + str(self.file)

    def selected(self):
        self._fresh()
        if self.file is None or not self.file.is_file() or self.file.is_symlink():
            raise ValueError("Сначала найдите и выберите файл. Контекст действует пять минут.")
        if not any(self.file.resolve().is_relative_to(root) for root in self.roots()):
            raise ValueError("Файл больше не находится в разрешённых папках поиска.")
        return self.file

    def open_selected(self, reveal=False):
        with self.lock:
            path = self.selected()
            if os.name != "nt":
                raise ValueError("Открытие файла доступно в Windows")
            if reveal:
                import subprocess

                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                if path.suffix.casefold() in {
                    ".exe",
                    ".msi",
                    ".cmd",
                    ".bat",
                    ".ps1",
                    ".py",
                    ".vbs",
                    ".lnk",
                }:
                    raise ValueError(
                        "Это исполняемый файл. Откройте его папку и запустите вручную после проверки."
                    )
                os.startfile(str(path))
            return "Windows получила запрос: " + str(path)

    def extract(self, stop=None):
        with self.lock:
            path = self.selected()
            if path.suffix.casefold() != ".zip":
                raise ValueError("Пока поддерживаются ZIP-архивы. Файл не изменён.")
            parent = self.folder or path.parent
            destination = parent / (path.stem + " — распаковано")
            if destination.exists():
                raise ValueError(
                    "Папка результата уже существует. Перезапись отключена: " + str(destination)
                )
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 2000 or sum(e.file_size for e in entries) > 256 * 1024**2:
                    raise ValueError("Для голосовой распаковки: до 2000 файлов и 256 МБ.")
                names = set()
                for entry in entries:
                    parts = entry.filename.replace("\\", "/").split("/")
                    canonical = "/".join(parts).casefold().rstrip("/")
                    if (
                        not parts[0]
                        or any(
                            p in (".", "..") or ":" in p or p.rstrip(" .") != p for p in parts if p
                        )
                        or canonical in names
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    ):
                        raise ValueError(
                            "В архиве небезопасные или повторяющиеся пути. Распаковка отменена."
                        )
                    names.add(canonical)
                stage = Path(tempfile.mkdtemp(prefix=".krest-unpack-", dir=parent))
                try:
                    for entry in entries:
                        if stop and stop.is_set():
                            raise ValueError("Распаковка отменена")
                        target = stage / entry.filename.replace("\\", "/")
                        if not target.resolve().is_relative_to(stage):
                            raise ValueError("Путь выходит за папку распаковки")
                        if entry.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(entry) as src, target.open("xb") as dst:
                                shutil.copyfileobj(src, dst, 64 * 1024)
                    if destination.exists():
                        raise ValueError("Папка результата появилась во время распаковки")
                    stage.rename(destination)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            self.folder, self.file = destination, None
            self.selected_at = time.monotonic()
            return "Архив распакован: " + str(destination)
