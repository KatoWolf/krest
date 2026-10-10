import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import os
import psutil


class DesktopAPI:
    """Load desktop bindings only when a desktop command is actually requested."""

    def __getattr__(self, name):
        import importlib

        return getattr(importlib.import_module("pyautogui"), name)


pyautogui = DesktopAPI()
import requests
import json
import subprocess
import threading
import time
import pyperclip
import random
import shutil
import re
import ctypes
import webbrowser
from datetime import datetime
import sys

# --- ИМПОРТЫ ДЛЯ НОВЫХ ФУНКЦИЙ ---
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

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

AFK_MODE = False
START_TIME = datetime.now()
LOGS = []
SHARED_QUEUE = None

# --- СИСТЕМА ФАЙЛОВ И КЭША ---
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

AESTHETICS_DIR = os.path.join(BASE_DIR, "aesthetics")
USED_AESTHETICS_DIR = os.path.join(AESTHETICS_DIR, "used")
CACHE_FILE = os.path.join(BASE_DIR, "steam_cache.json")
TEMP_DIR = os.path.join(BASE_DIR, "temp")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")


def add_log(text):
    now = datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{now}] {text}"
    LOGS.append(log_entry)
    print(f"--- [KREST LOG]: {log_entry} ---")
    if len(LOGS) > 15:
        LOGS.pop(0)


# --- ФУНКЦИИ АНТИ-БАН КЭША STEAM ---
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
    try:
        cache = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        cache[key] = {"time": time.time(), "data": data}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=4)
    except Exception as e:
        add_log(f"Ошибка сохранения кэша: {e}")


# --- ИНТЕРФЕЙС (ПАНЕЛЬ КНОПОК) ---
def get_main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(KeyboardButton("📊 Статус"), KeyboardButton("📸 Скрин ПК"))
    markup.add(KeyboardButton("🖥 Активное окно"), KeyboardButton("💾 Диски"))
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


def check_cs2_inventory(force_update=False):
    if not STEAM_ID:
        return "❌ Ошибка: В конфиге нет `STEAM_ID`."
    if not force_update:
        cached = get_cached_data("cs2_inv", 900)
        if cached:
            return cached + "\n\n_(⚡ Данные из кэша)_"

    try:
        r = requests.get(
            f"https://steamcommunity.com/inventory/{STEAM_ID}/730/2?l=russian&count=1000",
            timeout=10,
        )
        if r.status_code == 429:
            old_cache = get_cached_data("cs2_inv", 999999)
            if old_cache:
                return old_cache + "\n\n_(⚠️ Steam Rate Limit. Показаны старые данные)_"
            return "❌ Стим заблокировал запрос (Rate Limit)."
        if r.status_code != 200:
            return "❌ Инвентарь скрыт или API недоступен."

        data = r.json()
        if "descriptions" not in data:
            return "✅ Инвентарь пуст."

        locked_items = []
        now = datetime.utcnow()
        for desc in data["descriptions"]:
            if desc.get("tradable") == 0 and "cache_expiration" in desc:
                name = desc.get("market_name", desc.get("name", "Неизвестный предмет"))
                exp_str = desc["cache_expiration"]
                try:
                    exp_time = datetime.strptime(exp_str, "%Y-%m-%dT%H:%M:%SZ")
                    delta = exp_time - now
                    if delta.total_seconds() > 0:
                        locked_items.append(
                            f"🔒 `{name}`\n⏳ Бан спадёт через: **{delta.days}д {delta.seconds // 3600 % 24}ч**"
                        )
                except:
                    locked_items.append(f"🔒 `{name}`\n⏳ Дата разбана: **{exp_str}**")

        if not locked_items:
            res = "✅ В инвентаре CS2 нет предметов с трейд-баном."
        else:
            list_str = "\n\n".join(locked_items[:20]) + (
                f"\n\n...и еще {len(locked_items) - 20} шт." if len(locked_items) > 20 else ""
            )
            res = f"📦 **ИНВЕНТАРЬ CS2:**\n\n{list_str}"

        save_cached_data("cs2_inv", res)
        return res
    except Exception as e:
        return f"❌ Ошибка инвентаря: {e}"


def check_active_trades():
    if not STEAM_API_KEY:
        return "❌ Ошибка: Нет `STEAM_API_KEY`."
    cached = get_cached_data("active_trades", 300)
    if cached:
        return cached
    try:
        url = f"https://api.steampowered.com/IEconService/GetTradeOffers/v1/?key={STEAM_API_KEY}&get_received_offers=1&active_only=1"
        r = requests.get(url, timeout=10)
        if r.status_code == 429:
            return get_cached_data("active_trades", 999999) or "❌ Steam Rate Limit."
        data = r.json()
        offers = data.get("response", {}).get("trade_offers_received", [])
        if not offers:
            res = "🤝 Активных предложений обмена нет."
        else:
            res = f"🤝 **НОВЫЕ ТРЕЙДЫ!** У тебя **{len(offers)}** активных предложений на обмен."
        save_cached_data("active_trades", res)
        return res
    except:
        return "❌ Ошибка при проверке трейдов."


# --- ЗАГРУЗКА ФАЙЛОВ С ТЕЛЕФОНА НА ПК ---
@bot.message_handler(
    content_types=["document", "photo", "video", "audio"],
    func=lambda m: getattr(m.from_user, "id", None) == MY_ID,
)
def handle_remote_upload(message):
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
        bot.reply_to(message, f"❌ Ошибка приема файла: {e}")


# --- ОБРАБОТКА КОМАНД И ТЕКСТА ---
@bot.message_handler(
    content_types=["text"], func=lambda m: getattr(m.from_user, "id", None) == MY_ID
)
def terminal_handler(message):
    global AFK_MODE
    if SHARED_QUEUE is not None:
        SHARED_QUEUE.put_nowait("Telegram Terminal")
    text = message.text.lower().strip()

    # --- СКАЧИВАНИЕ МУЗЫКИ ИЗ ВИДЕО (НОВОЕ) ---
    if text.startswith("аудио ") or text.startswith("музыка "):
        if yt_dlp is None:
            bot.reply_to(message, "Установите yt-dlp и FFmpeg для скачивания аудио.")
            return
        url = message.text.split(" ", 1)[1].strip()
        m = bot.reply_to(message, "🎵 Инициирую перехват аудиодорожки...")
        try:
            ydl_opts = {
                "format": "m4a/bestaudio/best",  # Качаем чистый звук m4a (Идеально для ТГ)
                "outtmpl": os.path.join(TEMP_DIR, "downloaded_audio.%(ext)s"),
                "quiet": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

            with open(filename, "rb") as audio:
                bot.send_audio(message.chat.id, audio, caption="💠 Аудио успешно извлечено.")
            os.remove(filename)
        except Exception as e:
            bot.edit_message_text(f"❌ Ошибка загрузки аудио: {e}", message.chat.id, m.message_id)
        return

    # --- СКАЧИВАНИЕ ВИДЕО (TikTok, YT, Insta) ---
    media_regex = r"(https?://(?:www\.)?(?:tiktok\.com|youtube\.com|youtu\.be|instagram\.com|pinterest\.com|pin\.it)[^\s]+)"
    match = re.search(media_regex, message.text)

    # Условие: качаем видео ТОЛЬКО если это просто ссылка, а не команда "открой" или "ссылка"
    if match and not (text.startswith("ссылка ") or text.startswith("открой ")):
        url = match.group(1)
        m = bot.reply_to(message, "💠 Обнаружен медиа-линк. Инициирую перехват видео...")
        try:
            ydl_opts = {
                "outtmpl": os.path.join(TEMP_DIR, "downloaded_video.%(ext)s"),
                "format": "best[ext=mp4]/best",  # Строго mp4, чтобы ТГ не ругался
                "quiet": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

            with open(filename, "rb") as video:
                bot.send_video(
                    message.chat.id,
                    video,
                    caption="💠 Видео успешно извлечено из сети.",
                )
            os.remove(filename)
        except Exception as e:
            bot.edit_message_text(f"❌ Ошибка загрузки медиа: {e}", message.chat.id, m.message_id)
        return

    # 1. Меню
    if text in ["/start", "help", "команды", "меню", "🧠 меню команд"]:
        menu = (
            "💠 **KREST TERMINAL OS V3.0**\n───\n"
            "🕹 **Инфо и Удаленный Контроль:**\n"
            "● `статус` — Состояние ПК\n"
            "● `топ` или `процессы` — Топ-5 жрущих ОЗУ процессов\n"
            "● `диски` — Инфо о свободном месте\n"
            "● `окно` — Показать активное окно на экране\n"
            "● `ip` — Текущий IP и гео\n"
            "● `пинг [домен]` — Проверка сети\n"
            "───\n"
            "⚡ **Файлы, Ссылки и Медиа-перехват:**\n"
            "● `[Просто кинь ссылку на TikTok/YT]` — Скачает видео\n"
            "● `аудио [ссылка]` — Вырежет и скачает музыку из видео\n"
            "● `буфер` / `в буфер [текст]` — Управление буфером обмена\n"
            "● `папка [путь]` — Посмотреть файлы в папке\n"
            "● `файл [путь]` — Скачать файл с ПК на телефон\n"
            "● `загрузи [url]` — Заставить ПК скачать файл по ссылке\n"
            "───\n"
            "🖥 **Управление системой:**\n"
            "● `блок пк` / `рестарт пк` / `выключи пк`\n"
            "● `скажи [текст]` — Озвучить текст через колонки ПК\n"
            "● `напиши [текст]` — Имитация ввода с клавиатуры\n"
            "● `ссылка [url]` / `поиск [запрос]` — Открыть браузер на ПК\n"
            "● `запусти [steam/cs2]` / `убей [имя.exe]`\n"
            "● `алерт [текст]` / `cmd [код]`\n"
            "───\n"
            "🎵 **Разное:**\n"
            "● `пауза` / `громче` / `тише` / `некст`\n"
            "● `не спи` — AFK-защита мыши"
        )
        bot.reply_to(message, menu, parse_mode="Markdown", reply_markup=get_main_keyboard())

    # 2. Кнопки и Базовая Телеметрия
    elif "статус" in text:
        bot.reply_to(
            message,
            f"📊 **ОТЧЕТ ДЛЯ KATOWOLF:**\n{get_sys_info()}",
        )
    elif text in ["топ", "процессы", "нагрузка"]:
        bot.reply_to(message, get_top_processes(), parse_mode="Markdown")
    elif text == "диски" or text == "💾 диски":
        bot.reply_to(message, get_disk_info(), parse_mode="Markdown")
    elif text == "окно" or text == "🖥 активное окно":
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            active_title = buf.value if buf.value else "Рабочий стол / Скрыто"
            bot.reply_to(
                message,
                f"🖥 **АКТИВНОЕ ОКНО НА ПК:**\n`{active_title}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка чтения активного окна: {e}")
    elif text == "буфер" or text == "📋 буфер":
        try:
            cb = pyperclip.paste()
            res = cb[:4000] if cb else "Пусто"
            bot.reply_to(message, f"📋 **БУФЕР ОБМЕНА ПК:**\n`{res}`", parse_mode="Markdown")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка буфера: {e}")
    elif text.startswith("в буфер "):
        cb_text = message.text[8:]
        pyperclip.copy(cb_text)
        bot.reply_to(message, "✅ Текст скопирован в буфер обмена на ПК.")
    elif "инвентарь" in text:
        bot.send_chat_action(message.chat.id, "typing")
        bot.reply_to(message, check_cs2_inventory(force_update=False), parse_mode="Markdown")
    elif "трейды" in text:
        bot.send_chat_action(message.chat.id, "typing")
        bot.reply_to(message, check_active_trades(), parse_mode="Markdown")
    elif "крипта" in text or "курс" in text:
        bot.reply_to(message, get_crypto_rates(), parse_mode="Markdown")

    elif "скрин" in text:
        try:
            screen_name = f"Krest_View_{int(time.time())}.png"
            screen_path = os.path.join(TEMP_DIR, screen_name)
            pyautogui.screenshot(screen_path)
            with open(screen_path, "rb") as f:
                bot.send_photo(message.chat.id, f, caption="💠 Визуальный контакт.")
            os.remove(screen_path)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка захвата: {e}")

    elif "блок пк" in text or text == "блок":
        bot.reply_to(message, "💠 Блокирую систему. Увидимся, Wolf.")
        os.system("rundll32.exe user32.dll,LockWorkStation")

    # 3. Медиа, Файлы, Алерты, Утилиты
    elif text in ["пауза", "плей", "стоп", "🎵 плей / пауза"]:
        pyautogui.press("playpause")
        bot.reply_to(message, "🎵 Медиа: Пауза/Плей")
    elif text in ["громче", "звук +"]:
        pyautogui.press("volumeup", presses=5)
        bot.reply_to(message, "🔊 Громкость увеличена")
    elif text in ["тише", "звук -"]:
        pyautogui.press("volumedown", presses=5)
        bot.reply_to(message, "🔉 Громкость уменьшена")
    elif text in ["некст", "следующий"]:
        pyautogui.press("nexttrack")
        bot.reply_to(message, "⏭ Следующий трек")

    elif text.startswith("скажи "):
        phrase = message.text[6:].replace('"', " ").replace("'", " ")
        cmd = f"powershell -Command \"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{phrase}');\""
        threading.Thread(target=lambda: os.system(cmd)).start()
        bot.reply_to(message, f"🗣 На терминале озвучено:\n_{phrase}_", parse_mode="Markdown")

    elif text.startswith("алерт "):
        alert_msg = message.text[6:]
        threading.Thread(
            target=lambda: ctypes.windll.user32.MessageBoxW(
                0, alert_msg, "ВНИМАНИЕ! (KREST SYSTEM)", 0x1000
            )
        ).start()
        bot.reply_to(message, "💠 Сообщение выведено на монитор ПК.")

    elif text.startswith("ссылка ") or text.startswith("открой "):
        url = text.replace("ссылка ", "").replace("открой ", "").strip()
        if not url.startswith("http"):
            url = "https://" + url
        webbrowser.open(url)
        bot.reply_to(message, f"💠 Открываю `{url}` на терминале.", parse_mode="Markdown")

    elif text.startswith("поиск "):
        query = message.text[6:].strip().replace(" ", "+")
        webbrowser.open(f"https://www.google.com/search?q={query}")
        bot.reply_to(
            message,
            f"💠 Ищу в браузере: `{message.text[6:].strip()}`",
            parse_mode="Markdown",
        )

    elif text.startswith("папка ") or text.startswith("ls "):
        folder_path = message.text.split(" ", 1)[1].strip()
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            try:
                items = os.listdir(folder_path)
                if not items:
                    bot.reply_to(message, "📂 Папка пуста.")
                else:
                    files_list = "\n".join([f"📄 `{i}`" for i in items[:40]])
                    if len(items) > 40:
                        files_list += f"\n...и еще {len(items) - 40} файлов"
                    bot.reply_to(
                        message,
                        f"📂 **Содержимое папки:**\n`{folder_path}`\n\n{files_list}",
                        parse_mode="Markdown",
                    )
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка доступа: {e}")
        else:
            bot.reply_to(message, "❌ Папка не найдена или это файл.")

    elif text.startswith("загрузи "):
        url = message.text[8:].strip()
        bot.reply_to(message, f"⏳ Начинаю скачивание файла на ПК из сети...")
        try:
            r = requests.get(url, stream=True, timeout=30)
            filename = url.split("/")[-1] or f"download_{int(time.time())}"
            save_path = os.path.join(UPLOADS_DIR, filename)
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            bot.reply_to(
                message,
                f"✅ Файл загружен на диск!\n📂 Путь: `{save_path}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка скачивания: {e}")

    elif text.startswith("пинг "):
        host = message.text[5:].strip()
        bot.send_chat_action(message.chat.id, "typing")
        try:
            if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or host.startswith("-"):
                raise ValueError("Некорректный адрес")
            output = subprocess.check_output(
                ["ping", "-n" if os.name == "nt" else "-c", "4", host],
                text=True,
                errors="replace",
                timeout=15,
            )
            bot.reply_to(
                message,
                f"🌐 **Пинг {host}:**\n```\n{output}\n```",
                parse_mode="Markdown",
            )
        except (subprocess.SubprocessError, ValueError, OSError):
            bot.reply_to(message, f"❌ Не удалось пропинговать `{host}`.", parse_mode="Markdown")

    elif text.startswith("файл "):
        file_path = message.text[5:].strip()
        if os.path.exists(file_path):
            bot.send_chat_action(message.chat.id, "upload_document")
            try:
                with open(file_path, "rb") as doc:
                    bot.send_document(message.chat.id, doc, caption="💠 Файл извлечен.")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка чтения файла: {e}")
        else:
            bot.reply_to(message, "❌ Файл не найден. Проверь путь.")

    elif text.startswith("запусти "):
        app = text.replace("запусти ", "").strip()
        apps_map = {
            "steam": "start steam://open/main",
            "cs2": "start steam://rungameid/730",
            "калькулятор": "calc.exe",
            "блокнот": "notepad.exe",
        }
        cmd_run = apps_map.get(app, app)
        try:
            subprocess.Popen(cmd_run, shell=True)
            bot.reply_to(message, f"💠 Инициирован запуск: {app}")
        except:
            bot.reply_to(message, "❌ Не удалось запустить.")

    # 4. Доп. Команды управления
    elif text == "выключи пк":
        os.system("shutdown /s /t 30")
        bot.reply_to(
            message,
            "⚠️ Инициировано выключение ПК через 30 секунд. Чтобы отменить, напиши `отмени выключение`.",
            parse_mode="Markdown",
        )
    elif text == "рестарт пк" or text == "перезагрузи пк":
        os.system("shutdown /r /t 10")
        bot.reply_to(message, "⚠️ Инициирована перезагрузка терминала.", parse_mode="Markdown")
    elif text == "отмени выключение":
        os.system("shutdown /a")
        bot.reply_to(message, "✅ Процесс выключения/перезагрузки прерван.")
    elif text.startswith("напиши ") or text.startswith("введи "):
        text_to_type = message.text.split(" ", 1)[1]
        pyperclip.copy(text_to_type)
        pyautogui.hotkey("ctrl", "v")
        bot.reply_to(
            message,
            f"💠 Текст вставлен на ПК:\n_{text_to_type}_",
            parse_mode="Markdown",
        )

    elif text.startswith("cmd "):
        try:
            bot.send_chat_action(message.chat.id, "typing")
            output = subprocess.check_output(
                message.text[4:],
                shell=True,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            res_out = output[:4000] if output else "✅ Успешно"
            bot.reply_to(message, f"💻 **ТЕРМИНАЛ:**\n```\n{res_out}\n```", parse_mode="Markdown")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка консоли:\n```\n{e}\n```", parse_mode="Markdown")
    elif text == "ip":
        try:
            ip_data = requests.get("http://ip-api.com/json/", timeout=5).json()
            bot.reply_to(
                message,
                f"🌍 **СЕТЬ:**\nIP: `{ip_data['query']}`\nЛокация: {ip_data['city']}, {ip_data['country']}",
                parse_mode="Markdown",
            )
        except:
            bot.reply_to(message, "❌ Не удалось получить IP.")
    elif "не спи" in text:
        AFK_MODE = not AFK_MODE
        bot.reply_to(message, f"💠 AFK-защита: **{'АКТИВИРОВАНА' if AFK_MODE else 'ВЫКЛЮЧЕНА'}**")
    elif text.startswith("убей "):
        proc_name = text.replace("убей ", "").strip()
        c = 0
        if not proc_name:
            bot.reply_to(message, "Укажите точное имя процесса.")
            return
        for process in psutil.process_iter(["name"]):
            try:
                name = (process.info["name"] or "").lower().removesuffix(".exe")
                if process.pid != os.getpid() and name == proc_name.removesuffix(".exe"):
                    process.terminate()
                    c += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        bot.reply_to(message, f"💠 Убито процессов `{proc_name}`: {c}")

    else:
        bot.reply_to(message, "Команда не распознана. Откройте «Меню команд».")


# --- STARTUP LOGIC ---
def run_krest_service(shared_queue=None):
    if not TOKEN or not MY_ID:
        raise RuntimeError("Заполните токен TGT и Telegram ID владельца в меню.")
    bot.initialize(TOKEN)
    bot.get_me()  # Fail on invalid authentication before starting background workers.
    from bridge_sdk import emit

    emit("ready", "Telegram подтвердил токен бота")
    for directory in [AESTHETICS_DIR, USED_AESTHETICS_DIR, TEMP_DIR, UPLOADS_DIR]:
        os.makedirs(directory, exist_ok=True)
    global SHARED_QUEUE
    SHARED_QUEUE = shared_queue
    if shared_queue is not None:
        add_log("Connected to system notifications queue")

    # Отключаем фоновые посты/wishlist по твоему запросу (Оставлен только AFK шифт)
    def simple_bg():
        global AFK_MODE
        ticker = 0
        while True:
            time.sleep(1)
            ticker += 1
            if AFK_MODE and ticker % 40 == 0:
                pyautogui.press("shift")

    threading.Thread(target=simple_bg, daemon=True).start()
    print("--- [KREST TG TERMINAL]: STARTED ---")

    while True:
        try:
            bot.infinity_polling(timeout=20, logger_level=None)
        except requests.exceptions.ReadTimeout:
            print("--- [TG TERMINAL]: Потеряна связь. Переподключение... ---")
            time.sleep(3)
        except Exception as e:
            print(f"--- [TG TERMINAL FATAL]: {e} ---")
            time.sleep(5)


if __name__ == "__main__":
    from notifications import NotificationQueue

    run_krest_service(NotificationQueue())
