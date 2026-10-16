import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import os
import psutil

import requests
import json
import time
import shutil
import threading
from datetime import datetime

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
        self.callbacks = []

    def message_handler(self, **filters):
        def decorator(callback):
            self.handlers.append((callback, filters))
            return callback

        return decorator

    def callback_query_handler(self, **filters):
        def decorator(callback):
            self.callbacks.append((callback, filters))
            return callback

        return decorator

    def initialize(self, token):
        if self.client is not None:
            return
        self.client = telebot.TeleBot(token)
        for callback, filters in self.handlers:
            self.client.register_message_handler(callback, **filters)
        for callback, filters in self.callbacks:
            self.client.register_callback_query_handler(callback, **filters)

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
CACHE_LOCK = threading.Lock()
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")


def add_log(text):
    now = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{now}] {text}"
    LOGS.append(log_entry)
    print(f"--- [KREST LOG]: {log_entry} ---")
    if len(LOGS) > 15:
        LOGS.pop(0)


# --- КЭШ STEAM: ОГРАНИЧЕНИЕ ПОВТОРНЫХ ЗАПРОСОВ ---
def get_cached_data(key, max_age_seconds):
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
                if key in cache:
                    if time.time() - cache[key]["time"] < max_age_seconds:
                        return cache[key]["data"]
    except Exception as e:
        add_log(f"Ошибка чтения кэша: {e}")
    return None


def save_cached_data(key, data):
    from storage import atomic_write_json, read_json

    try:
        with CACHE_LOCK:
            try:
                cache = read_json(CACHE_FILE)
            except (ValueError, OSError):
                cache = {}
            cache[key] = {"time": time.time(), "data": data}
            atomic_write_json(CACHE_FILE, cache)
    except OSError:
        add_log("Кэш Telegram Terminal не сохранён")


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
        res = "🔝 Топ процессов по памяти\n"
        for name, mem in procs:
            res += f"● {name}: {mem:.1f}%\n"
        return res
    except Exception as e:
        return f"❌ Ошибка телеметрии: {e}"


# --- ДИСКИ ---
def get_disk_info():
    import string

    try:
        res = "💾 Состояние дисков\n\n"
        available_drives = ["%s:" % d for d in string.ascii_uppercase if os.path.exists("%s:" % d)]
        for drive in available_drives:
            try:
                total, used, free = shutil.disk_usage(drive)
                res += f"● {drive} | Свободно: {free // (2**30)} ГБ / {total // (2**30)} ГБ\n"
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


def _steam_data(resource, fetch, *, force=False):
    """Reuse the monitor snapshot and persist cooldowns between terminal requests."""
    import hashlib
    from steam_service import SteamError, read_cache

    manager = ConfigManager()
    keys = manager.snapshot()["api_keys"]
    state = read_cache(manager.filepath.parent / "steam_state.json")
    if state.get("account") != keys["steam_id"]:
        state = {}
    if resource == "730:2":
        cached = state.get("inventories", {}).get(resource, {})
        value, updated = cached.get("items"), cached.get("updated", 0)
    else:
        value, updated = state.get(resource), state.get(resource + "_updated", 0)
    cooling = time.time() < state.get("retry_after", {}).get(resource, 0)
    if value is not None and ((not force and time.time() - updated < 900) or cooling):
        stamp = datetime.fromtimestamp(updated).strftime("%d.%m %H:%M")
        return value, "\nСохранённые данные · " + stamp + (
            ". Steam временно недоступен." if cooling else ""
        )
    if cooling:
        raise SteamError(state.get("errors", {}).get(resource, "Steam временно недоступен"))
    fingerprint = hashlib.sha256(keys["steam_api_key"].encode()).hexdigest()[:16]
    cooldown_key = "cooldown:" + keys["steam_id"] + ":" + fingerprint + ":" + resource
    cooldown = get_cached_data(cooldown_key, 86400)
    if isinstance(cooldown, dict) and time.time() < cooldown.get("until", 0):
        raise SteamError(cooldown["message"])
    try:
        return fetch(), ""
    except SteamError as exc:
        save_cached_data(
            cooldown_key, {"until": time.time() + max(120, exc.retry_after), "message": str(exc)}
        )
        raise


def check_cs2_inventory(force_update=False):
    from steam_service import SteamClient, SteamError, inventory_summary

    keys = ConfigManager().snapshot()["api_keys"]
    steam_id = keys["steam_id"]
    cache_key = "cs2_inv:" + steam_id
    cached = get_cached_data(cache_key, 900)
    if cached and not force_update:
        return cached + "\nДанные из кэша (до 15 минут)."
    try:
        items, note = _steam_data(
            "730:2",
            lambda: SteamClient(keys["steam_api_key"], steam_id).inventory("730", "2"),
            force=force_update,
        )
        counts = inventory_summary(items)
        result = (
            f"CS2 · предметов: {counts['total']}\n"
            f"Доступны для обмена: {counts['tradable']}\n"
            f"Недоступны: {counts['locked']} · Статус неизвестен: {counts['unknown']}"
        )
        locked = [item for item in items if item.get("tradable") is False]
        for item in locked[:15]:
            date = (
                datetime.fromtimestamp(item["unlock_at"]).strftime("%d.%m.%Y %H:%M")
                if item.get("unlock_at")
                else "Steam не сообщил срок"
            )
            result += "\n" + item["name"][:100] + " · " + date
        if locked:
            result += "\nНедоступность обмена не всегда означает временную блокировку."
        save_cached_data(cache_key, result)
        return result + note
    except (SteamError, ValueError, KeyError, TypeError) as exc:
        old = get_cached_data(cache_key, 86400)
        reason = (
            str(exc)
            if isinstance(exc, SteamError)
            else "Проверьте SteamID64 и видимость инвентаря."
        )
        return (
            (old + "\nПоказаны прежние данные. " + reason)
            if old
            else "Не удалось проверить инвентарь. " + reason
        )


def check_active_trades():
    from steam_service import SteamClient, SteamError

    keys = ConfigManager().snapshot()["api_keys"]
    cache_key = "active_trades:" + keys["steam_id"]
    cached = get_cached_data(cache_key, 300)
    if cached:
        return cached + "\nДанные из кэша (до 5 минут)."
    try:
        offers, note = _steam_data(
            "offers", lambda: SteamClient(keys["steam_api_key"], keys["steam_id"]).offers()
        )
        result = f"Предложения обмена: входящих {sum(o['direction'] == 'in' for o in offers)}, исходящих {sum(o['direction'] == 'out' for o in offers)}."
        save_cached_data(cache_key, result)
        return result + note
    except (SteamError, ValueError, KeyError, TypeError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, SteamError)
            else "Проверьте Steam Web API Key и доступность Steam."
        )
        return "Не удалось проверить обмены. " + reason


# --- ЗАГРУЗКА ФАЙЛОВ С ТЕЛЕФОНА НА ПК ---
@bot.message_handler(
    content_types=["document", "photo", "video", "audio"],
    func=lambda m: getattr(m.from_user, "id", None) == MY_ID,
)
def handle_remote_upload(message):
    from configuration import ConfigManager
    from integrations import redact

    if (
        getattr(message.from_user, "id", None) != MY_ID
        or getattr(message.chat, "type", "private") != "private"
    ):
        return
    if not ConfigManager().snapshot().get("remote_control", {}).get("allow_uploads", False):
        bot.reply_to(message, "Приём файлов выключен в меню Крест.")
        return

    try:
        from kernel_client import KernelClient

        content = message.document or message.video or message.audio
        if content is None and message.photo:
            content = message.photo[-1]
        if content is None:
            return
        if not 0 <= (getattr(content, "file_size", 0) or 0) <= 20 * 1024 * 1024:
            bot.reply_to(message, "Размер файла превышает 20 МБ.")
            return
        suffix = (
            ".jpg"
            if message.photo
            else ".mp4"
            if message.video
            else ".mp3"
            if message.audio
            else ".bin"
        )
        name = getattr(content, "file_name", None) or f"upload_{message.message_id}{suffix}"
        KernelClient(ConfigManager().filepath).call(
            "autonomy.receive",
            file_id=content.file_id,
            filename=name,
            sender=message.from_user.id,
            message_id=message.message_id,
        )
        bot.reply_to(
            message,
            "Приняла файл в очередь. Сохраню в uploads, когда ПК будет свободен. Результат — в разделе «Дежурство» пульта и меню Крест.",
        )
    except Exception as exc:
        bot.reply_to(
            message,
            "Не удалось поставить файл в очередь: "
            + redact(str(exc), ConfigManager().snapshot())[:700],
        )


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
    original = (message.text or "").strip()
    aliases = {
        "/status": "статус",
        "/screen": "скриншот",
        "/history": "история команд",
        "📸 скрин пк": "скриншот",
        "🖥 активное окно": "окно",
    }
    original = aliases.get(original.casefold(), original)
    text = original.casefold()
    if text in ("/start", "/help", "/menu", "help", "команды", "меню", "🧠 меню команд"):
        from terminal_menu import menu

        body, keyboard = menu(cfg)
        bot.reply_to(message, body, reply_markup=keyboard)
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
        if text == "история команд":
            from kernel_client import KernelClient
            from terminal_jobs import history_text

            rows = KernelClient(settings.filepath).call("terminal_jobs", chat_id=message.chat.id)
            bot.reply_to(message, redact(history_text(rows), cfg)[:4000])
            return
        if text == "скриншот":
            from screenshots import capture_screen

            path = capture_screen()
            with path.open("rb") as image:
                bot.send_document(message.chat.id, image, caption="Крест · экран ПК")
            return
        if text == "окно":
            from performance import foreground_title

            bot.reply_to(message, foreground_title() or "Нет доступного активного окна")
            return
        if text == "файлы" or text.startswith("файл "):
            from pathlib import Path

            root = settings.filepath.parent
            roots = [root / "uploads", root / "screenshots"]
            if text == "файлы":
                names = [
                    str(p.relative_to(root))
                    for folder in roots
                    if folder.exists()
                    for p in sorted(folder.iterdir(), key=lambda p: p.name)[:40]
                    if p.is_file() and not p.is_symlink()
                ]
                bot.reply_to(message, "Файлы пульта:\n" + ("\n".join(names)[:3700] or "Пока пусто"))
            else:
                path = (root / original.split(" ", 1)[1]).resolve(strict=True)
                if (
                    not any(path.is_relative_to(folder.resolve()) for folder in roots)
                    or not path.is_file()
                ):
                    raise ValueError("Выберите файл из uploads или screenshots")
                if path.stat().st_size > 49 * 1024 * 1024:
                    raise ValueError("Передача файлов до 49 МБ")
                with Path(path).open("rb") as document:
                    bot.send_document(message.chat.id, document)
            return
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
        accepted = client.call("remote_submit", original=original, request_key=key)
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
        from terminal_jobs import watcher

        watched = watcher.add(
            key,
            lambda request_key: client.call("job_result", request_key=request_key),
            lambda result: bot.reply_to(message, redact("Отчёт: " + result, cfg)[:4000]),
        )
        bot.reply_to(
            message,
            "Действие ещё выполняется. "
            + ("Пришлю результат, когда закончу." if watched else "Результат появится в /history.")
            + " Повторять команду не нужно.",
        )
    except Exception as exc:
        bot.reply_to(message, "Не удалось выполнить: " + redact(str(exc), cfg)[:2000])


@bot.callback_query_handler(func=lambda call: getattr(call.from_user, "id", None) == MY_ID)
def terminal_callback(call):
    from types import SimpleNamespace
    from terminal_menu import menu, callback_command

    if (
        getattr(call.from_user, "id", None) != MY_ID
        or getattr(getattr(call, "message", None), "chat", None) is None
        or call.message.chat.type != "private"
    ):
        return
    bot.answer_callback_query(call.id)
    value = call.data or ""
    if value.startswith("menu:"):
        body, keyboard = menu(ConfigManager().snapshot(), value.split(":", 1)[1])
        try:
            bot.edit_message_text(
                body, call.message.chat.id, call.message.message_id, reply_markup=keyboard
            )
        except telebot.apihelper.ApiTelegramException as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return
    command = callback_command(value)
    if command:
        terminal_handler(
            SimpleNamespace(
                text=command,
                from_user=call.from_user,
                chat=call.message.chat,
                message_id="button:" + str(call.id),
            )
        )


def run_krest_service(shared_queue=None):
    if not TOKEN or not MY_ID:
        raise RuntimeError("Заполните токен TGT и Telegram ID владельца в меню.")
    bot.initialize(TOKEN)
    bot.get_me()  # Fail on invalid authentication before starting background workers.
    from telebot.types import BotCommand

    try:
        bot.set_my_commands(
            [
                BotCommand("menu", "Пульт компьютера"),
                BotCommand("status", "Состояние ПК"),
                BotCommand("screen", "Скриншот ПК"),
                BotCommand("help", "Доступные команды"),
            ]
        )
    except Exception:
        add_log("Меню Telegram не обновилось; /menu остаётся доступно")
    from bridge_sdk import emit

    emit("ready", "Telegram подтвердил токен бота")
    auth_lost = threading.Event()

    def check_connection():
        while not auth_lost.wait(45):
            try:
                bot.get_me()
                emit("health", "Telegram Bot API отвечает; пульт доступен", connected=True)
            except Exception as exc:
                if getattr(exc, "error_code", None) in (401, 403):
                    emit("auth_required", "Telegram отклонил токен пульта. Проверьте его в меню.")
                    auth_lost.set()
                    bot.stop_polling()
                    return
                emit(
                    "health",
                    "Telegram временно недоступен; polling восстановит соединение",
                    connected=False,
                )

    threading.Thread(target=check_connection, name="TelegramTerminalHealth", daemon=True).start()

    for directory in [UPLOADS_DIR]:
        os.makedirs(directory, exist_ok=True)
    global SHARED_QUEUE
    SHARED_QUEUE = shared_queue
    if shared_queue is not None:
        add_log("Connected to system notifications queue")

    print("--- [KREST TG TERMINAL]: STARTED ---")

    while not auth_lost.is_set():
        try:
            bot.infinity_polling(timeout=20, logger_level=None)
        except requests.exceptions.ReadTimeout:
            print("--- [TG TERMINAL]: Потеряна связь. Переподключение... ---")
            time.sleep(3)
        except Exception as e:
            print(f"--- [TG TERMINAL FATAL]: {e} ---")
            time.sleep(5)


if __name__ == "__main__":
    from licensing import require_premium
    from configuration import ConfigManager

    require_premium(ConfigManager().snapshot())
    from notifications import NotificationQueue

    run_krest_service(NotificationQueue())
