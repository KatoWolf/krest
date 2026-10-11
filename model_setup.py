"""First-run speech assets from fixed official HTTPS locations; existing models win."""

from pathlib import Path
import os
import shutil
import tempfile
import urllib.request
import zipfile
import time

from app_paths import data_dir
from voice_models import discover_models, is_vosk, is_silero

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


def _download_checked(url, target, progress, cancelled):
    for attempt in range(3):
        if cancelled():
            raise InterruptedError("Подготовка голоса остановлена")
        try:
            download(url, target, progress, cancelled)
            return
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError):
            target.unlink(missing_ok=True)
            if attempt == 2:
                raise
            progress(f"Загрузка прервалась. Повтор {attempt + 2} из 3…")
            for _ in range(10 * (attempt + 1)):
                if cancelled():
                    raise InterruptedError("Подготовка голоса остановлена")
                time.sleep(0.1)


def prepare(
    settings,
    progress=lambda text: None,
    cancelled=lambda: False,
    *,
    components=("vosk_path", "silero_path"),
    force=False,
    destination=None,
):
    models = discover_models(settings)
    requested = set(components)
    if not requested <= {"vosk_path", "silero_path"}:
        raise ValueError("Неизвестный компонент голоса")
    if not force and (
        os.name != "nt" or not settings.get("app_settings", {}).get("voice_auto_models", True)
    ):
        return models
    if all(models[key] for key in requested):
        return models
    from process_lock import ProcessLock

    folder = Path(destination) if destination is not None else data_dir() / "models"
    folder.mkdir(parents=True, exist_ok=True)
    lock = ProcessLock(folder / ".download.lock")
    if not lock.acquire():
        raise RuntimeError("Модели уже подготавливаются. Подключусь после завершения.")
    try:
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=folder) as temp:
            temp = Path(temp)
            for key, filename, url, title, valid in (
                ("vosk_path", "vosk-model-small-ru-0.22", VOSK_URL, "распознавание Vosk", is_vosk),
                ("silero_path", "v4_ru.pt", SILERO_URL, "голос Silero", is_silero),
            ):
                if key not in requested or models[key]:
                    continue
                target = folder / filename
                if not valid(target):
                    progress("Подготовка: загружаю " + title + "…")
                    try:
                        archive = temp / (filename + ".download")
                        _download_checked(url, archive, progress, cancelled)
                        if cancelled():
                            raise InterruptedError("Подготовка голоса остановлена")
                        source = (
                            unpack_vosk(archive, temp / "unpacked")
                            if key == "vosk_path"
                            else archive
                        )
                        if not valid(source):
                            raise ValueError("Файл модели повреждён или имеет неподходящий формат")
                        # Only replace a broken managed cache; user-supplied files
                        # outside this folder are preserved.
                        if target.exists():
                            target.rename(folder / (filename + ".broken-" + str(time.time_ns())))
                        shutil.move(str(source), target)
                    except InterruptedError:
                        raise
                    except (OSError, ValueError, RuntimeError) as exc:
                        message = "Не удалось подготовить " + title + ": " + str(exc)[:180]
                        if force:
                            raise RuntimeError(message) from exc
                        progress(message + ". Загрузка будет повторена автоматически.")
                        continue
                models[key] = str(target.resolve())
        return models
    finally:
        lock.close()
