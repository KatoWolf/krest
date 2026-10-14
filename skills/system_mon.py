import os
import sys
import time
import shutil
import ctypes
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

import psutil

from skill_manager import skill  # type: ignore # pyright: ignore

logger = logging.getLogger("SkillSystemMon")

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004

# Категории папок со всеми поддерживаемыми расширениями файлов
DOWNLOAD_CATEGORIES: Dict[str, List[str]] = {
    "Images_Krest": [
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".svg",
        ".ico",
        ".tiff",
        ".heic",
        ".raw",
        ".jfif",
        ".pjpeg",
        ".pjp",
        ".avif",
    ],
    "Video_Krest": [
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
        ".3gp",
        ".mpeg",
        ".mpg",
        ".ts",
        ".m2ts",
    ],
    "Audio_Krest": [
        ".mp3",
        ".wav",
        ".flac",
        ".aac",
        ".ogg",
        ".m4a",
        ".wma",
        ".opus",
        ".mid",
        ".midi",
        ".alac",
        ".aiff",
    ],
    "Documents_Krest": [
        ".pdf",
        ".docx",
        ".doc",
        ".xlsx",
        ".xls",
        ".pptx",
        ".ppt",
        ".txt",
        ".rtf",
        ".csv",
        ".odt",
        ".ods",
        ".odp",
        ".epub",
        ".fb2",
        ".mobi",
        ".djvu",
        ".log",
    ],
    "Archives_Krest": [
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".iso",
        ".bz2",
        ".xz",
        ".cab",
        ".tgz",
        ".zipx",
        ".z",
        ".arj",
    ],
    "Programs_Krest": [
        ".exe",
        ".msi",
        ".bat",
        ".cmd",
        ".ps1",
        ".vbs",
        ".apk",
        ".dmg",
        ".appimage",
        ".deb",
        ".rpm",
    ],
    "Torrents_Krest": [".torrent"],
    "Dev_Krest": [
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".html",
        ".css",
        ".scss",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".java",
        ".json",
        ".xml",
        ".sql",
        ".php",
        ".sh",
        ".pyw",
        ".yml",
        ".yaml",
        ".cs",
        ".go",
        ".rs",
        ".rb",
    ],
    "Design_Krest": [
        ".psd",
        ".ai",
        ".fig",
        ".blend",
        ".max",
        ".obj",
        ".fbx",
        ".stl",
        ".cdr",
        ".eps",
        ".xd",
        ".aep",
        ".prproj",
        ".c4d",
    ],
}

# Расширения временных и незавершенных загрузок
INCOMPLETE_DOWNLOAD_EXTENSIONS = {
    ".crdownload",
    ".tmp",
    ".part",
    ".download",
    ".!ut",
    ".!qb",
    ".unconfirmed",
}


def empty_recycle_bin() -> bool:
    """Очищает корзину Windows без подтверждения и звуков."""
    if os.name != "nt":
        return False
    try:
        flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
        return result == 0
    except Exception as e:
        logger.error(f"Ошибка при очистке корзины через Shell API: {e}")
        return False


def clean_directory_files(folder_path: Path, min_age_seconds=86400, stop=None) -> int:
    folder_path = Path(folder_path)
    if not folder_path.is_dir() or folder_path.is_symlink():
        return 0
    cutoff = time.time() - min_age_seconds
    freed = 0
    for current, dirs, files in os.walk(folder_path, followlinks=False):
        if stop is not None and stop.is_set():
            break
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            if stop is not None and stop.is_set():
                return freed
            path = Path(current) / name
            try:
                info = path.lstat()
                if path.is_symlink() or info.st_mtime > cutoff:
                    continue
                path.unlink()
                freed += info.st_size
            except OSError:
                continue
    return freed


def try_get_gpu_info() -> Dict[str, Any]:
    """Use a bounded subprocess; GPUtil's internal nvidia-smi has no timeout."""
    gpu_data = {}

    # Резервный запрос через nvidia-smi CLI
    try:
        cmd = "nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total --format=csv,noheader,nounits"
        output = subprocess.check_output(
            cmd.split(), stderr=subprocess.DEVNULL, text=True, timeout=3
        )
        parts = [p.strip() for p in output.strip().splitlines()[0].split(",")]
        if len(parts) >= 4:
            gpu_data = {
                "load_percent": int(parts[0]),
                "temperature": int(parts[1]),
                "memory_used_mb": int(parts[2]),
                "memory_total_mb": int(parts[3]),
            }
    except Exception:
        pass

    return gpu_data


def get_all_disks_status() -> List[Dict[str, Any]]:
    """Собирает данные по всем подключенным дискам в системе."""
    disks = []
    for partition in psutil.disk_partitions(all=False):
        if os.name == "nt" and (
            any(kind in partition.opts for kind in ("cdrom", "remote"))
            or partition.mountpoint.startswith("\\\\")
        ):
            continue
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append(
                {
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "total_gb": round(usage.total / (1024**3), 1),
                    "used_gb": round(usage.used / (1024**3), 1),
                    "free_gb": round(usage.free / (1024**3), 1),
                    "percent": int(usage.percent),
                }
            )
        except Exception:
            continue
    return disks


def get_unique_filepath(target_dir: Path, filename: str) -> Path:
    """Генерирует уникальный путь к файлу, предотвращая перезапись."""
    file_path = target_dir / filename
    if not file_path.exists():
        return file_path

    stem = file_path.stem
    suffix = file_path.suffix
    counter = 1

    while True:
        new_path = target_dir / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def get_category_for_extension(ext: str) -> str:
    """Определяет целевую папку *_Krest на основе расширения файла."""
    ext_lower = ext.lower()
    for category_folder, extensions in DOWNLOAD_CATEGORIES.items():
        if ext_lower in extensions:
            return category_folder
    return "Other_Krest"


@skill(
    keywords=[
        "состояние системы",
        "загрузка пк",
        "мониторинг",
        "нагрузка системы",
        "статус пк",
        "проверь ресурсы",
    ],
    description="Подробная статистика по загрузке ЦП, ОЗУ, накопителям и видеокарте",
    category="System",
)
def get_system_status(text: str = "") -> Dict[str, Any]:
    """Возвращает актуальный статус нагрузки всей системы."""
    cpu_usage = psutil.cpu_percent(interval=0.3)

    ram = psutil.virtual_memory()
    ram_data = {
        "percent": int(ram.percent),
        "used_gb": round(ram.used / (1024**3), 1),
        "total_gb": round(ram.total / (1024**3), 1),
    }

    disks_data = get_all_disks_status()
    gpu_data = try_get_gpu_info()

    status_info = {
        "cpu_usage_percent": int(cpu_usage),
        "ram": ram_data,
        "disks": disks_data,
        "gpu": gpu_data,
    }

    logger.info(
        f"Статус системы: CPU {cpu_usage}%, RAM {ram_data['percent']}%, Disks: {len(disks_data)}"
    )
    return status_info


@skill(
    keywords=[
        "очистка темп",
        "убери мусор",
        "очисти диски",
        "очистить временные файлы",
        "удали временные файлы",
    ],
    description="Очистка временных файлов Windows старше суток",
    category="System",
)
def clean_temp_files(text: str = "", state=None) -> Dict[str, Any]:
    """Быстрая молчаливая очистка всех временных каталогов."""
    total_freed_bytes = 0
    stop = getattr(getattr(state, "runtime", None), "command_stop", None)

    # 1. Пользовательская папка %TEMP%
    user_temp = os.environ.get("TEMP")
    if user_temp:
        total_freed_bytes += clean_directory_files(Path(user_temp), stop=stop)

    # 2. Системная папка Windows Temp
    win_root = os.environ.get("SystemRoot", "C:\\Windows")
    win_temp = Path(win_root) / "Temp"
    total_freed_bytes += clean_directory_files(win_temp, stop=stop)

    # 3. Дополнительные дампы и кэши
    crash_dumps = Path(win_root) / "CrashDumps"
    if crash_dumps.exists():
        total_freed_bytes += clean_directory_files(crash_dumps, stop=stop)

    # 4. Очистка Корзины
    bin_cleared = False  # Recycle bin has a separate explicit command.

    freed_mb = round(total_freed_bytes / (1024 * 1024), 2)
    if stop is not None and stop.is_set():
        return {"status": "error", "message": f"Очистка остановлена. Освобождено {freed_mb} МБ."}
    logger.info(f"Очистка завершена. Освобождено {freed_mb} МБ. Корзина очищена: {bin_cleared}")

    return {
        "freed_bytes": total_freed_bytes,
        "freed_mb": freed_mb,
        "recycle_bin_cleared": bin_cleared,
    }


@skill(
    keywords=[
        "кто жрет память",
        "тяжелые процессы",
        "топ процессов",
        "загрузка процессов",
        "что ест память",
    ],
    description="Вывод процессов с наибольшим потреблением ресурсов",
    category="System",
)
def get_top_processes(text: str = "") -> List[Dict[str, Any]]:
    """Формирует список TOP процессов по оперативной памяти."""
    processes = []
    ignored_names = {"system", "idle", "registry", "memory compression"}

    for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = proc.info
            name = info.get("name") or ""
            if name.lower() in ignored_names:
                continue

            mem_info = info.get("memory_info")
            if not mem_info:
                continue

            mem_mb = round(mem_info.rss / (1024 * 1024), 1)
            if mem_mb > 50:
                processes.append(
                    {
                        "pid": info["pid"],
                        "name": name.replace(".exe", "").capitalize(),
                        "memory_mb": mem_mb,
                        "cpu_percent": info.get("cpu_percent", 0.0),
                    }
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue

    processes.sort(key=lambda x: x["memory_mb"], reverse=True)
    top_list = processes[:5]

    logger.info(f"Топ процессов по RAM: {[p['name'] for p in top_list]}")
    return top_list


@skill(
    keywords=[
        "заряд батареи",
        "состояние аккумулятора",
        "сколько процентов батареи",
        "статус батареи",
    ],
    description="Проверка аккумулятора и статуса питания",
    category="System",
)
def get_battery_status(text: str = "") -> Dict[str, Any]:
    """Проверяет параметры аккумулятора."""
    battery = psutil.sensors_battery()

    if not battery:
        logger.info("Батарея в системе не обнаружена.")
        return {"has_battery": False}

    result = {
        "has_battery": True,
        "percent": int(battery.percent),
        "power_plugged": battery.power_plugged,
        "secs_left": battery.secsleft if battery.secsleft != psutil.POWER_TIME_UNLIMITED else -1,
    }

    logger.info(f"Статус батареи: {result['percent']}%, в сети: {result['power_plugged']}")
    return result


@skill(
    keywords=[
        "аптайм",
        "время работы пк",
        "сколько работает комп",
        "как долго включен пк",
    ],
    description="Расчет точного времени работы компьютера с последней перезагрузки",
    category="System",
)
def get_system_uptime(text: str = "") -> Dict[str, int]:
    """Возвращает время работы системы."""
    boot_timestamp = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_timestamp)

    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60

    result = {
        "total_seconds": uptime_seconds,
        "days": days,
        "hours": hours,
        "minutes": minutes,
    }

    logger.info(f"Uptime ПК: {days} дн. {hours} ч. {minutes} мин.")
    return result


@skill(
    keywords=[
        "рассортируй загрузки",
        "отсортируй загрузки",
        "наведи порядок в загрузках",
        "отсортируй файлы",
        "разложи загрузки",
        "уберись в загрузках",
        "сортировка загрузок",
    ],
    description="Автоматическая молчаливая раскладка всех файлов в Загрузках по категориям *_Krest",
    category="System",
)
def organize_downloads(text: str = "") -> Dict[str, Any]:
    """Use the same conservative sorter as the background worker."""
    from app_state import get_state
    from downloads import DownloadSorter

    folder = (
        get_state()
        .config_getter()
        .get("app_settings", {})
        .get("downloads_path", str(Path.home() / "Downloads"))
    )
    try:
        state = get_state()
        stop = state.runtime.command_stop if hasattr(state, "runtime") else None
        result = DownloadSorter(state.config_getter().get("file_policy", {})).scan(
            folder, automatic=False, stop=stop
        )
        if stop and stop.is_set():
            return dict(
                result,
                status="error",
                message=f"Сортировка остановлена. Перемещено: {result['moved_count']}; остальные файлы сохранены.",
            )
        if result["errors"]:
            return dict(
                result,
                status="error",
                message=(
                    f"Перемещено: {result['moved_count']}. Ошибки: "
                    + "; ".join(result["errors"][:3])
                ),
            )
        return dict(result, status="success")
    except (OSError, ValueError) as exc:
        return {"status": "error", "message": str(exc), "moved_count": 0}
