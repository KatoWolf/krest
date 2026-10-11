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


class TerminalHealth:
    """Observe existing polling requests; never add a network health probe."""

    def __init__(self):
        self.state = None

    def waiting(self):
        from bridge_sdk import emit

        self.state = "unknown"
        emit(
            "status",
            "Токен Telegram подтверждён; ожидается первый ответ опроса сообщений",
            health="unknown",
        )

    def failed(self, exc):
        from bridge_sdk import emit

        code = getattr(exc, "error_code", None)
        message = {
            401: "Telegram отклонил токен Terminal. Проверьте токен бота.",
            409: "Telegram отклонил опрос сообщений: конфликт другого опроса или webhook (409).",
            429: "Telegram ограничил частоту запросов; SDK ожидает повторного подключения.",
        }.get(code, "Связь Telegram Terminal не подтверждена; опрос сообщений будет повторён.")
        if self.state != message:
            self.state = message
            emit("error", message)

    def confirmed(self):
        from bridge_sdk import emit

        if self.state != "ready":
            self.state = "ready"
            emit("ready", "Telegram Terminal подтвердил получение обновлений")


class ObservedTeleBot(telebot.TeleBot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.health = TerminalHealth()

    def get_updates(self, *args, **kwargs):
        # TeleBot's internal retry loop calls this public method as well.
        try:
            updates = super().get_updates(*args, **kwargs)
        except Exception as exc:
            self.health.failed(exc)
            raise
        self.health.confirmed()
        return updates


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
        self.client = ObservedTeleBot(token)
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


def is_current_owner(message, cfg):
    """Validate each request against the currently saved owner, never import-time IDs."""
    owner = str(cfg.get("api_keys", {}).get("telegram_owner_id", "")).strip()
    sender = getattr(getattr(message, "from_user", None), "id", None)
    chat = getattr(message, "chat", None)
    return (
        1 <= len(owner) <= 20
        and owner.isascii()
        and owner.isdecimal()
        and int(owner) > 0
        and isinstance(sender, int)
        and not isinstance(sender, bool)
        and sender == int(owner)
        and getattr(chat, "type", None) == "private"
        and getattr(chat, "id", None) == sender
    )


# --- ЗАГРУЗКА ФАЙЛОВ С ТЕЛЕФОНА НА ПК ---
@bot.message_handler(
    content_types=["document", "photo", "video", "audio"],
)
def handle_remote_upload(message):
    from configuration import ConfigManager

    cfg = ConfigManager().snapshot()
    if not is_current_owner(message, cfg):
        return
    if not cfg.get("remote_control", {}).get("allow_uploads", False):
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
            limit = 20 * 1024 * 1024
            declared_size = getattr(file_info, "file_size", None)
            if declared_size is not None and (
                not isinstance(declared_size, int)
                or isinstance(declared_size, bool)
                or not 0 <= declared_size <= limit
            ):
                bot.reply_to(message, "Размер файла некорректен или превышает 20 МБ.")
                return
            downloaded_file = bot.download_file(file_info.file_path)
            if not isinstance(downloaded_file, (bytes, bytearray)):
                bot.reply_to(message, "Telegram вернул некорректные данные файла.")
                return
            if len(downloaded_file) > limit:
                bot.reply_to(message, "Фактический размер файла превышает 20 МБ.")
                return
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
@bot.message_handler(content_types=["text"])
def terminal_handler(message):
    from configuration import ConfigManager
    from integrations import redact
    from remote_policy import authorize

    settings = ConfigManager()
    cfg = settings.snapshot()
    if not is_current_owner(message, cfg):
        return
    original = message.text or ""
    if not isinstance(original, str) or len(original) > 2000:
        bot.reply_to(message, "Команда слишком длинная. Максимум — 2000 символов.")
        return
    text = original.strip().casefold()
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
    health = getattr(bot, "health", None) or TerminalHealth()
    try:
        bot.get_me()  # Fail on invalid authentication before starting background workers.
    except Exception as exc:
        health.failed(exc)
        raise
    health.waiting()
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
            return  # The SDK returns after an intentional stop (including Ctrl+C).
        except requests.exceptions.ReadTimeout as exc:
            health.failed(exc)
            print("--- [TG TERMINAL]: Потеряна связь. Переподключение... ---")
            time.sleep(3)
        except Exception as e:
            health.failed(e)
            print(f"--- [TG TERMINAL FATAL]: {type(e).__name__} ---")
            time.sleep(5)


if __name__ == "__main__":
    from licensing import require_premium
    from configuration import ConfigManager

    require_premium(ConfigManager().snapshot())
    from notifications import NotificationQueue

    run_krest_service(NotificationQueue())
