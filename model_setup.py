"""First-run speech assets from fixed official HTTPS locations; existing models win."""

from pathlib import Path
import os
import shutil
import tempfile
import urllib.request
import zipfile

from app_paths import data_dir
from voice_models import discover_models, is_vosk

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
SILERO_URL = "https://models.silero.ai/models/tts/ru/v4_ru.pt"
LIMIT = 160 * 1024 * 1024


def download(url, target, progress, cancelled):
    request = urllib.request.Request(url, headers={"User-Agent": "Krest-Speech-Setup/1"})
    with urllib.request.urlopen(request, timeout=20) as response, open(target, "wb") as output:
        # No arbitrary mirror supplied by configuration or a plugin.
        from urllib.parse import urlparse

        if (
            urlparse(response.geturl()).scheme != "https"
            or urlparse(response.geturl()).hostname != urlparse(url).hostname
        ):
            raise RuntimeError("Сервер перенаправил модель на другой адрес")
        expected = int(response.headers.get("Content-Length", "0"))
        total = 0
        last_mb = -1
        while True:
            if cancelled():
                raise InterruptedError("Подготовка голоса остановлена")
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > LIMIT:
                raise ValueError("Файл модели превышает допустимый размер")
            output.write(chunk)
            mb = total // (1024 * 1024)
            if mb // 5 != last_mb:
                last_mb = mb // 5
                progress(f"Подготовка голоса: скачано {mb} МБ…")
        if expected and expected != total:
            raise ValueError("Загрузка модели оборвалась. Подключусь повторно.")
        if total < 1024:
            raise ValueError("Сервер вернул пустую или неполную модель")


def unpack_vosk(archive, destination):
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        if len(entries) > 2000 or sum(e.file_size for e in entries) > 500 * 1024 * 1024:
            raise ValueError("Некорректный архив Vosk")
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or ":" in name
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Небезопасный путь в архиве модели")
        z.extractall(destination)
    models = [p for p in Path(destination).iterdir() if p.is_dir() and is_vosk(p)]
    if len(models) != 1:
        raise ValueError("Архив не содержит одну целую модель Vosk")
    return models[0]


def prepare(settings, progress=lambda text: None, cancelled=lambda: False):
    models = discover_models(settings)
    if os.name != "nt" or not settings.get("app_settings", {}).get("voice_auto_models", True):
        return models
    if all(models.values()):
        return models
    from process_lock import ProcessLock

    folder = data_dir() / "models"
    folder.mkdir(parents=True, exist_ok=True)
    lock = ProcessLock(folder / ".download.lock")
    if not lock.acquire():
        raise RuntimeError("Модели уже подготавливаются. Подключусь после завершения.")
    try:
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=folder) as temp:
            temp = Path(temp)
            if not models["vosk_path"]:
                target = folder / "vosk-model-small-ru-0.22"
                if not is_vosk(target):
                    progress("Первый запуск: загружаю русскую модель распознавания Vosk…")
                    download(VOSK_URL, temp / "vosk.zip", progress, cancelled)
                    source = unpack_vosk(temp / "vosk.zip", temp / "unpacked")
                    if target.exists():
                        raise RuntimeError(
                            "Папка модели уже существует, но неполна: " + str(target)
                        )
                    source.rename(target)
            if not models["silero_path"]:
                target = folder / "v4_ru.pt"
                if not target.exists():
                    progress("Первый запуск: загружаю голос Silero…")
                    try:
                        download(SILERO_URL, temp / "silero.pt", progress, cancelled)
                        if cancelled():
                            raise InterruptedError("Подготовка голоса остановлена")
                        if not zipfile.is_zipfile(temp / "silero.pt"):
                            raise ValueError("Сервер вернул неполный пакет Silero")
                        shutil.move(str(temp / "silero.pt"), target)
                    except InterruptedError:
                        raise
                    except (OSError, ValueError, RuntimeError) as exc:
                        progress(
                            "Распознавание готово. Голос Silero пока не скачан: " + str(exc)[:160]
                        )
        return discover_models(settings)
    finally:
        lock.close()
