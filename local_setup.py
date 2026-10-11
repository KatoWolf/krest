"""Read-only local discovery. No downloads, microphone capture or credential guessing."""

import copy
import os
from pathlib import Path


def downloads_folder(home=None):
    explicit_home = home is not None
    home = Path(home or Path.home())
    if os.name == "nt" and not explicit_home:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            candidate = Path(os.path.expandvars(value))
            if candidate.is_dir():
                return candidate
        except (OSError, TypeError):
            pass
    return home / "Downloads"


def discover_settings(config, base_dir=None, home=None):
    from app_paths import program_dir

    root = Path(base_dir) if base_dir is not None else program_dir()
    folder = downloads_folder(home)
    home = Path(home or Path.home())
    result = copy.deepcopy(config)
    settings = result["app_settings"]
    changes = []

    def use(key, path, label):
        value = str(path)
        if settings.get(key) != value:
            settings[key] = value
            changes.append(label)

    current = settings.get("downloads_path", "")
    if current in ("", str(home / "Downloads")) and folder.is_dir():
        use("downloads_path", folder, "Папка загрузок")

    from voice_models import discover_models

    models = discover_models(result, root)
    for key, label in (("vosk_path", "Модель распознавания"), ("silero_path", "Модель голоса")):
        if models[key]:
            use(key, models[key], label)

    if not result["steam"].get("install_path"):
        from skills.webo import get_steam_install_path

        steam = get_steam_install_path()
        if steam:
            result["steam"]["install_path"] = str(steam)
            changes.append("Установка Steam")
    return result, changes


def readiness(config, base_dir=None):
    from app_paths import program_dir

    root = Path(base_dir) if base_dir is not None else program_dir()
    cfg = config["app_settings"]
    vosk = root / cfg["vosk_path"]
    silero = root / cfg["silero_path"]
    lines = [
        "Микрофон: "
        + ("системный, выбирается автоматически" if not cfg["microphone"] else "выбран вручную"),
        "Плеер: "
        + (
            "автоматически по воспроизведению"
            if not config["media"]["preferred_app"]
            else "закреплён вручную"
        ),
        "Распознавание: "
        + (
            "файлы модели найдены"
            if (vosk / "am").is_dir() and (vosk / "conf").is_dir()
            else "добавьте модель Vosk в папку программы"
        ),
        "Голос: "
        + (
            "файл модели найден"
            if silero.is_file()
            else "добавьте файл модели Silero в папку программы"
        ),
        "Steam: "
        + (
            "папка найдена"
            if config["steam"]["install_path"]
            else "будет найден при установке клиента"
        ),
    ]
    return "\n".join(lines)
