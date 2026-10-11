import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import os
import psutil

import requests
import json
import time
import shutil
import threading
from storage import atomic_write_json
from steam_service import SteamClient, SteamError, inventory_summary
from datetime import datetime, timezone

# --- ИНИЦИАЛИЗАЦИЯ КОНФИГА ---
try:
    import config

    TOKEN = config.KREST_BOT_TOKEN
    MY_ID = config.MY_ID
    CHANNEL_ID = config.MY_CHANNEL_ID
    STEAM_ID = getattr(config, "STEAM_ID", None)
    STEAM_API_KEY = getattr(config, "STEAM_API_KEY", None)
except ImportError:
    print("Ошибка: Создай config.py с TOKEN, MY_ID, CHANNEL_ID, STEAM_ID и STEAM_API_KEY!")
    exit()


class BotProxy:
    def __init__(self):
        self.client = None
        self.handlers = []

    def message_handler(self, **filters):
        def decorator(callback):
            self.handlers.append((callback, filters))
            return callback

        return decorator

    def initialize(self, token):
        if self.client is not None:
            return
        self.client = telebot.TeleBot(token)
        for callback, filters in self.handlers:
            self.client.register_message_handler(callback, **filters)

    def __getattr__(self, name):
        if self.client is None:
            raise RuntimeError("Telegram Terminal не запущен")
        return getattr(self.client, name)


bot = BotProxy()

START_TIME = datetime.now()
LOGS = []
SHARED_QUEUE = None

# --- СИСТЕМА ФАЙЛОВ И КЭША ---
from configuration import ConfigManager

BASE_DIR = str(ConfigManager().filepath.parent)
CACHE_FILE = os.path.join(BASE_DIR, "steam_cache.json")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
_CACHE_LOCK = threading.RLock()


def add_log(text):
    now = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{now}] {text}"
    LOGS.append(log_entry)
    print(f"--- [KREST LOG]: {log_entry} ---")
    if len(LOGS) > 15:
        LOGS.pop(0)


# --- КЭШ STEAM: ОГРАНИЧЕНИЕ ПОВТОРНЫХ ЗАПРОСОВ ---
def _cache_account():
    return str(STEAM_ID or "")


def get_cached_data(key, max_age_seconds):
    try:
        with _CACHE_LOCK, open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("account") != _cache_account():
            return None
        entry = cache.get(key, {})
        age = time.time() - entry.get("time", 0)
        if 0 <= age < max_age_seconds:
            return entry.get("data")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def save_cached_data(key, data):
    try:
        with _CACHE_LOCK:
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                if not isinstance(cache, dict) or cache.get("account") != _cache_account():
                    cache = {}
            except (OSError, ValueError):
                cache = {}
            cache["account"] = _cache_account()
            cache[key] = {"time": time.time(), "data": data}
            atomic_write_json(CACHE_FILE, cache)
    except OSError:
        add_log("Не удалось сохранить кэш Steam.")


# --- ИНТЕРФЕЙС (ПАНЕЛЬ КНОПОК) ---
def get_main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(KeyboardButton("📊 Статус"), KeyboardButton("План на сегодня"))
    markup.add(KeyboardButton("Список таймеров"), KeyboardButton("💾 Диски"))
    markup.add(KeyboardButton("📦 Инвентарь CS2"), KeyboardButton("🤝 Трейды"))
    markup.add(KeyboardButton("🎵 Плей / Пауза"), KeyboardButton("🧠 Меню команд"))
    return markup


# --- ТОП ПРОЦЕССОВ ---
def get_top_processes():
    try:
        procs = []
        for p in psutil.process_iter(["name", "memory_percent"]):
            try:
                if p.info["name"] and p.info["memory_percent"] is not None:
                    procs.append((p.info["name"], p.info["memory_percent"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs = sorted(procs, key=lambda x: x[1], reverse=True)[:5]
        res = "🔝 **Топ процессов (по ОЗУ):**\n"
        for name, mem in procs:
            res += f"● `{name}`: {mem:.1f}%\n"
        return res
    except Exception as e:
        return f"❌ Ошибка телеметрии: {e}"


# --- ДИСКИ ---
def get_disk_info():
    import string

    try:
        res = "💾 **СОСТОЯНИЕ ДИСКОВ:**\n\n"
        available_drives = ["%s:" % d for d in string.ascii_uppercase if os.path.exists("%s:" % d)]
        for drive in available_drives:
            try:
                total, used, free = shutil.disk_usage(drive)
                res += f"● **{drive}** | Свободно: {free // (2**30)} ГБ / {total // (2**30)} ГБ\n"
            except:
                pass
        return res
    except Exception as e:
        return f"❌ Ошибка проверки дисков: {e}"


# --- КРИПТОВАЛЮТЫ ---
def get_crypto_rates():
    try:
        url = 'https://api.binance.com/api/v3/ticker/price?symbols=["BTCUSDT","ETHUSDT","SOLUSDT","TONUSDT"]'
        r = requests.get(url, timeout=5).json()
        res = "💰 **БИРЖЕВАЯ СВОДКА:**\n\n"
        for coin in r:
            name = coin["symbol"].replace("USDT", "")
            price = float(coin["price"])
            res += f"● **{name}**: ${price:,.2f}\n"
        return res
    except Exception as e:
        return f"❌ Ошибка получения курсов: {e}"


# --- STEAM ФУНКЦИИ С КЭШЕМ ---
def get_sys_info():
    cpu, ram = psutil.cpu_percent(), psutil.virtual_memory().percent
    game_list = [
        "rustclient.exe",
        "cs2.exe",
        "hl2.exe",
        "bannerlord.exe",
        "starcitizen.exe",
    ]
    active_game = next(
        (
            p.info["name"]
            for p in psutil.process_iter(["name"])
            if p.info["name"] and p.info["name"].lower() in game_list
        ),
        "НЕТ",
    )
    uptime = str(datetime.now() - START_TIME).split(".")[0]
    return f"🚀 CPU: {cpu}% | RAM: {ram}%\n🎮 Игра: {active_game}\n⏱ Uptime: {uptime}"


def _steam_failure(key, exc):
    cached = get_cached_data(key, 7 * 86400)
    reason = str(exc) if isinstance(exc, SteamError) else "Steam вернул некорректные данные."
    if cached:
        return cached + "\n\n⚠️ Старые данные (до 7 дней). Обновление не удалось: " + reason
    return "❌ " + reason


def check_cs2_inventory(force_update=False):
    if not STEAM_ID:
        return "❌ Укажите SteamID64 в настройках Steam."
    cached = None if force_update else get_cached_data("cs2_inv", 900)
    if cached:
        return cached + "\n\n⚡ Данные из кэша (не старше 15 минут)."
    try:
        items = SteamClient(STEAM_API_KEY, STEAM_ID).inventory("730", "2")
        summary = inventory_summary(items)
        lines = [
            "📦 Инвентарь CS2",
            f"Всего: {summary['total']} · Обмен доступен: {summary['tradable']}",
            f"Ограничены: {summary['locked']} · Статус неизвестен: {summary['unknown']}",
        ]
        restricted = [item for item in items if item["tradable"] is False]
        for item in restricted[:20]:
            unlock = item.get("unlock_at")
            date = (
                datetime.fromtimestamp(unlock, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
                if unlock
                else "Steam не указал дату"
            )
            lines.append(f"🔒 {item['name']} ×{item['amount']} · {date}")
        if len(restricted) > 20:
            lines.append(f"Ещё ограниченных позиций: {len(restricted) - 20}.")
        result = "\n".join(lines)
        save_cached_data("cs2_inv", result)
        return result
    except (SteamError, ValueError, KeyError, TypeError) as exc:
        return _steam_failure("cs2_inv", exc)


def check_active_trades():
    if not STEAM_API_KEY:
        return "❌ Укажите Steam Web API Key в настройках Steam."
    cached = get_cached_data("active_trades", 300)
    if cached:
        return cached + "\n\n⚡ Данные из кэша (не старше 5 минут)."
    try:
        offers = SteamClient(STEAM_API_KEY, STEAM_ID).offers()
        incoming = sum(offer["direction"] == "in" for offer in offers)
        outgoing = sum(offer["direction"] == "out" for offer in offers)
        result = (
            f"🤝 Активные предложения обмена: входящих {incoming}, исходящих {outgoing}."
            if offers
            else "🤝 Активных предложений обмена нет."
        )
        save_cached_data("active_trades", result)
        return result
    except (SteamError, ValueError, KeyError, TypeError) as exc:
        return _steam_failure("active_trades", exc)


# --- ЗАГРУЗКА ФАЙЛОВ С ТЕЛЕФОНА НА ПК ---
@bot.message_handler(
    content_types=["document", "photo", "video", "audio"],
    func=lambda m: getattr(m.from_user, "id", None) == MY_ID,
)
def handle_remote_upload(message):
    from configuration import ConfigManager

    if (
        getattr(message.from_user, "id", None) != MY_ID
        or getattr(message.chat, "type", "private") != "private"
    ):
        return
    if not ConfigManager().snapshot().get("remote_control", {}).get("allow_uploads", False):
        bot.reply_to(message, "Приём файлов выключен в меню Крест.")
        return

    try:
        bot.send_chat_action(message.chat.id, "upload_document")
        file_info = None
        file_name = f"upload_{int(time.time())}"

        if message.document:
            file_info = bot.get_file(message.document.file_id)
            file_name = os.path.basename(
                (message.document.file_name or file_name).replace("\\", "/")
            )
            if file_name in (".", ".."):
                file_name = f"upload_{time.time_ns()}"
        elif message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            file_name += ".jpg"
        elif message.video:
            file_info = bot.get_file(message.video.file_id)
            file_name += ".mp4"
        elif message.audio:
            file_info = bot.get_file(message.audio.file_id)
            file_name += ".mp3"

        if file_info:
            if not 0 <= (file_info.file_size or 0) <= 20 * 1024 * 1024:
                bot.reply_to(message, "Размер файла превышает 20 МБ.")
                return
            downloaded_file = bot.download_file(file_info.file_path)
            save_path = os.path.join(UPLOADS_DIR, file_name)
            if os.path.exists(save_path):
                stem, extension = os.path.splitext(save_path)
                save_path = f"{stem}_{time.time_ns()}{extension}"
            with open(save_path, "xb") as new_file:
                new_file.write(downloaded_file)
            bot.reply_to(
                message,
                f"📥 **ФАЙЛ ПОЛУЧЕН!**\nУспешно сохранен на ПК.\n📂 Путь: `{save_path}`",
                parse_mode="Markdown",
            )
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка приема файла: {type(e).__name__}")


# --- ОБРАБОТКА КОМАНД И ТЕКСТА ---
@bot.message_handler(
    content_types=["text"], func=lambda m: getattr(m.from_user, "id", None) == MY_ID
)
def terminal_handler(message):
    from configuration import ConfigManager
    from integrations import redact
    from remote_policy import authorize

    if (
        getattr(message.from_user, "id", None) != MY_ID
        or getattr(message.chat, "type", "private") != "private"
    ):
        return
    settings = ConfigManager()
    cfg = settings.snapshot()
    text = (message.text or "").strip().casefold()
    if text in ("/start", "help", "команды", "меню", "🧠 меню команд"):
        remote = cfg["remote_control"]
        body = "Крест · доступ владельца\n"
        body += (
            "Удалённые команды включены.\n"
            if remote["enabled"]
            else "Включите удалённые команды в Система → Доступ и восстановление.\n"
        )
        body += "Разрешённые команды:\n" + (
            "\n".join(remote["allowed_commands"]) or "Список пока пуст."
        )
        bot.reply_to(message, body[:4000], reply_markup=get_main_keyboard())
        return
    allowed, reason = authorize(text, cfg)
    if not allowed:
        bot.reply_to(message, reason)
        return
    readers = {
        "статус": get_sys_info,
        "📊 статус": get_sys_info,
        "диски": get_disk_info,
        "💾 диски": get_disk_info,
        "топ": get_top_processes,
        "процессы": get_top_processes,
        "крипта": get_crypto_rates,
        "📦 инвентарь cs2": check_cs2_inventory,
        "инвентарь": check_cs2_inventory,
        "🤝 трейды": check_active_trades,
        "трейды": check_active_trades,
    }
    try:
        if text in readers:
            bot.reply_to(message, redact(str(readers[text]()), cfg)[:4000])
            return
        from kernel_client import KernelClient
        import uuid

        client = KernelClient(settings.filepath)
        key = (
            "telegram:"
            + str(message.chat.id)
            + ":"
            + str(getattr(message, "message_id", uuid.uuid4().hex))
        )
        accepted = client.call("remote_submit", original=text, request_key=key)
        if not accepted:
            bot.reply_to(message, "Очередь заполнена. Повторите позже.")
            return
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = client.call("job_result", request_key=key)
            if job and job["status"] not in ("queued", "running"):
                bot.reply_to(message, redact(job["result"], cfg)[:4000] or "Действие завершено.")
                return
            time.sleep(0.15)
        bot.reply_to(message, "Действие ещё выполняется. Проверьте журнал ядра перед повтором.")
    except (OSError, ValueError, RuntimeError) as exc:
        bot.reply_to(message, "Не удалось выполнить: " + redact(str(exc), cfg)[:2000])


def run_krest_service(shared_queue=None):
    if not TOKEN or not MY_ID:
        raise RuntimeError("Заполните токен TGT и Telegram ID владельца в меню.")
    bot.initialize(TOKEN)
    bot.get_me()  # Fail on invalid authentication before starting background workers.
    from bridge_sdk import emit

    emit("ready", "Telegram подтвердил токен бота")
    for directory in [UPLOADS_DIR]:
        os.makedirs(directory, exist_ok=True)
    global SHARED_QUEUE
    SHARED_QUEUE = shared_queue
    if shared_queue is not None:
        add_log("Connected to system notifications queue")

    print("--- [KREST TG TERMINAL]: STARTED ---")

    while True:
        try:
            bot.infinity_polling(timeout=20, logger_level=None)
        except requests.exceptions.ReadTimeout:
            print("--- [TG TERMINAL]: Потеряна связь. Переподключение... ---")
            time.sleep(3)
        except Exception as e:
            print(f"--- [TG TERMINAL FATAL]: {type(e).__name__} ---")
            time.sleep(5)


if __name__ == "__main__":
    from licensing import require_premium
    from configuration import ConfigManager

    require_premium(ConfigManager().snapshot())
    from notifications import NotificationQueue

    run_krest_service(NotificationQueue())
