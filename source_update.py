"""Opt-in source updates with clean-tree checks, fast-forward only and a rollback ref."""

from pathlib import Path
import re
import subprocess


class SourceUpdater:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config

    def git(self, *args):
        result = subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True, timeout=45
        )
        if result.returncode:
            raise ValueError("Git: " + result.stderr.strip()[:1000])
        return result.stdout.strip()

    def check(self):
        self.git("rev-parse", "--show-toplevel")
        remote = self.git("remote", "get-url", "origin")
        if not remote:
            raise ValueError("Не настроен origin")
        self.git("fetch", "origin", "main")
        current = self.git("rev-parse", "HEAD")
        target = self.git("rev-parse", "FETCH_HEAD")
        return {
            "current": current,
            "target": target,
            "available": int(self.git("rev-list", "--count", current + ".." + target)) > 0,
            "summary": self.git("log", "--oneline", "-8", current + ".." + target),
        }

    def _offline(self):
        from process_lock import ProcessLock

        locks = [
            ProcessLock(self.config.filepath.parent / name) for name in (".kernel.lock", ".ui.lock")
        ]
        acquired = []
        for lock in locks:
            if not lock.acquire():
                for held in acquired:
                    held.close()
                raise ValueError("Закройте меню и завершите ядро перед обновлением")
            acquired.append(lock)
        return acquired

    def apply(self):
        from backup_service import create_backup
        from storage import atomic_write_json
        from datetime import datetime

        locks = self._offline()
        try:
            if self.git("status", "--porcelain"):
                raise ValueError(
                    "Есть локальные изменения исходников. Сохраните их перед обновлением."
                )
            state = self.check()
            if not state["available"]:
                return "Уже установлена текущая версия"
            self.git("merge-base", "--is-ancestor", state["current"], state["target"])
            backup = self.config.filepath.parent / (
                "before-update-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip"
            )
            create_backup(self.config, backup)
            atomic_write_json(
                self.config.filepath.parent / "update_rollback.json",
                {"previous": state["current"], "updated": state["target"], "backup": backup.name},
            )
            self.git("merge", "--ff-only", state["target"])
            return "Исходники обновлены. Установите зависимости выбранного режима и выполните python main.py --check."
        finally:
            for lock in locks:
                lock.close()

    def rollback(self):
        import json

        locks = self._offline()
        try:
            state = json.loads((self.config.filepath.parent / "update_rollback.json").read_text())
            if any(
                not re.fullmatch("[0-9a-f]{40,64}", state.get(key, ""))
                for key in ("previous", "updated")
            ):
                raise ValueError("Некорректная точка отката")
            if (
                self.git("status", "--porcelain")
                or self.git("rev-parse", "HEAD") != state["updated"]
            ):
                raise ValueError(
                    "После обновления появились изменения. Автоматический откат остановлен."
                )
            self.git("reset", "--hard", state["previous"])
            return "Предыдущие исходники восстановлены. Пользовательские данные сохранены."
        finally:
            for lock in locks:
                lock.close()
