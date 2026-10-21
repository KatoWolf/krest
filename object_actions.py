"""Explicit Explorer selection and streaming file actions, without screen capture."""

from datetime import datetime
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
import zipfile

from autonomy_files import (
    CHUNK,
    DISK_RESERVE,
    MAX_BYTES,
    MAX_FILES,
    clean_path,
    fingerprint,
    is_link,
)


def explorer_selection():
    """Called inside the bounded desktop helper, on its COM thread."""
    import win32gui
    from comtypes.client import CreateObject

    foreground = win32gui.GetForegroundWindow()
    shell = CreateObject("Shell.Application", dynamic=True)
    for window in shell.Windows():
        try:
            if int(window.HWND) != foreground:
                continue
            folder = window.Document.Folder.Self.Path
            selected = window.Document.SelectedItems()
            if selected.Count > 100:
                raise ValueError("Выберите не более 100 объектов")
            return {
                "folder": folder,
                "paths": [selected.Item(i).Path for i in range(selected.Count)],
            }
        except AttributeError:
            continue
    return {"folder": "", "paths": []}


class ObjectActions:
    def __init__(self, runtime, root, probe=None):
        self.runtime, self.root, self.probe = runtime, Path(root), probe
        self.lock = threading.RLock()
        self.context = {}
        self.selected_at = 0

    def select(self, paths, folder=""):
        if not isinstance(paths, list) or len(paths) > 100:
            raise ValueError("Выберите от 1 до 100 файлов или папок")
        paths = list(dict.fromkeys(str(clean_path(p)) for p in paths))
        if any(not Path(p).exists() for p in paths):
            raise ValueError("Один из выбранных объектов больше не существует")
        if folder:
            folder = str(clean_path(folder))
            if not Path(folder).is_dir():
                raise ValueError("Папка недоступна")
        with self.lock:
            self.context = {"paths": paths, "folder": folder}
            self.selected_at = time.monotonic()
            return dict(self.context)

    def current(self, *, foreground=True):
        if foreground:
            if self.probe:
                found = self.probe()
            elif os.name == "nt":
                from desktop_transport import request

                response = request({"operation": "file_context"}, timeout=4)
                if response.get("error"):
                    raise ValueError(response["error"])
                found = response.get("result", {})
            else:
                found = {}
            if found.get("folder"):
                return self.select(found.get("paths", []), found["folder"])
        with self.lock:
            if time.monotonic() - self.selected_at > 300:
                self.context = {}
            if not self.context:
                raise ValueError(
                    "Выделите файлы в Проводнике или выберите их в меню «Автоматизация»"
                )
            return self.select(self.context.get("paths", []), self.context.get("folder", ""))

    def folder(self):
        context = self.current()
        paths = context["paths"]
        if len(paths) == 1 and Path(paths[0]).is_dir():
            return paths[0]
        if context["folder"]:
            return context["folder"]
        raise ValueError("Откройте папку в Проводнике или выберите её в меню")

    def latest_screenshot(self, open_file=True):
        folder = self.root / "screenshots"
        files = [p for p in folder.glob("*.png") if p.is_file() and not is_link(p)]
        if not files:
            raise ValueError("Сохранённых скриншотов пока нет")
        path = max(files, key=lambda p: p.stat().st_mtime_ns)
        self.select([str(path)], str(folder))
        if open_file:
            if os.name != "nt":
                raise ValueError("Открытие скриншота доступно в Windows: " + str(path))
            os.startfile(str(path))
        return "Последний скриншот: " + str(path)

    def open(self, reveal=False):
        context = self.current()
        if len(context["paths"]) != 1:
            raise ValueError("Выберите один файл или папку")
        path = clean_path(context["paths"][0])
        if os.name != "nt":
            raise ValueError("Открытие объекта доступно в Windows")
        if reveal:
            import subprocess

            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        else:
            if path.suffix.casefold() in {
                ".exe",
                ".msi",
                ".msix",
                ".cmd",
                ".bat",
                ".ps1",
                ".py",
                ".vbs",
                ".lnk",
                ".url",
                ".hta",
                ".com",
                ".scr",
                ".reg",
                ".js",
            }:
                raise ValueError("Для запуска этого файла откройте его папку и запустите вручную")
            os.startfile(str(path))
        return "Открываю: " + str(path)

    def pack(self, foreground=True):
        context = self.current(foreground=foreground)
        sources = [clean_path(p) for p in context["paths"]]
        if not sources:
            raise ValueError("Выделите файлы или папки, которые нужно упаковать")
        # Ignore children explicitly selected together with their parent.
        sources = [p for p in sources if not any(p != q and p.is_relative_to(q) for q in sources)]
        names = [p.name.casefold() for p in sources]
        if len(set(names)) != len(names):
            raise ValueError("У выбранных объектов совпадают имена. Упакуйте их отдельно")
        destination = clean_path(self.root / "archives")
        if any(destination.is_relative_to(p) for p in sources):
            raise ValueError("Выберите отдельные файлы вне папки, содержащей архивы Крест")
        destination.mkdir(parents=True, exist_ok=True)
        name = "Крест_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S_") + uuid.uuid4().hex[:8]
        target = destination / (name + ".zip")
        partial = target.with_suffix(".zip.partial")
        stop = self.runtime.command_stop

        def guard():
            if stop.is_set():
                raise ValueError("Архивирование отменено")
            if shutil.disk_usage(destination).free < DISK_RESERVE + CHUNK:
                raise ValueError("Для архива нужен резерв 2 ГБ свободного места")

        def entries():
            count = total = 0
            for source in sources:
                if source.is_file():
                    iterator = [(source.parent, [], [source.name])]
                else:
                    iterator = os.walk(source, followlinks=False)
                for parent, dirs, files in iterator:
                    guard()
                    if any(is_link(Path(parent) / n) for n in dirs + files):
                        raise ValueError("В выбранной папке есть ссылки; архивирование отменено")
                    if not files and not dirs and source.is_dir():
                        yield Path(parent), Path(parent).relative_to(source.parent).as_posix() + "/"
                    for filename in files:
                        path = clean_path(Path(parent) / filename)
                        if not path.is_file():
                            raise ValueError("Объект не является обычным файлом: " + str(path))
                        count += 1
                        total += path.stat().st_size
                        if count > MAX_FILES or total > MAX_BYTES:
                            raise ValueError("За одну команду: до 10 000 файлов и 20 ГБ")
                        yield path, path.relative_to(source.parent).as_posix()

        try:
            with zipfile.ZipFile(partial, "x", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for path, relative in entries():
                    guard()
                    if path.is_dir():
                        archive.writestr(relative, b"")
                        continue
                    stamp = fingerprint(path)
                    with (
                        path.open("rb") as source,
                        archive.open(relative, "w", force_zip64=True) as output,
                    ):
                        while True:
                            guard()
                            block = source.read(CHUNK)
                            if not block:
                                break
                            output.write(block)
                    if fingerprint(path) != stamp:
                        raise ValueError("Файл изменился при упаковке: " + path.name)
            with zipfile.ZipFile(partial) as archive:
                for row in archive.infolist():
                    with archive.open(row) as source:
                        while True:
                            guard()
                            if not source.read(CHUNK):
                                break
            guard()
            # A fresh random target; exclusive commit also protects a concurrent writer.
            try:
                os.link(partial, target)
            except FileExistsError:
                raise
            except OSError:
                if os.name != "nt":
                    raise
                # Windows rename also refuses to replace a concurrent target
                # and works on FAT/exFAT volumes without hard links.
                os.rename(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        self.select([str(target)], str(destination))
        return "Архив создан и проверен: " + str(target)
