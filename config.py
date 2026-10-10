import os
import json

# ==========================================
# 0. Базовые пути проекта и динамический конфиг
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from configuration import CONFIG_PATH

CONFIG_JSON_PATH = str(CONFIG_PATH)


def _load_user_config():
    from configuration import ConfigManager

    return ConfigManager().config


_user_cfg = _load_user_config()


def reload_config() -> dict:
    """Перезагрузка конфигурации из файла во время работы приложения."""
    global _user_cfg
    _user_cfg = _load_user_config()
    return _user_cfg


def get_owner_name() -> str:
    """Динамическое получение имени владельца из config.json."""
    cfg = _load_user_config()
    return cfg.get("owner_name", "КатоВульф")


# ==========================================
# 1. Пути к языковым моделям STT / TTS и ресурсам
# ==========================================
MODEL_VOSK_PATH = os.path.join(
    BASE_DIR, _user_cfg.get("app_settings", {}).get("vosk_path", "model")
)
MODEL_SILERO_PATH = os.path.join(
    BASE_DIR, _user_cfg.get("app_settings", {}).get("silero_path", "model_silero.pt")
)
TIMER_ALARM_SOUND = os.path.join(BASE_DIR, "alarm.wav")
ICON_ICO_PATH = os.path.join(BASE_DIR, "icon.ico")
ICON_PNG_PATH = os.path.join(BASE_DIR, "icon.png")

# ==========================================
# 2. Основные параметры ассистента и логики
# ==========================================
ASSISTANT_NAME = "Крест"
WAKE_WORD = _user_cfg.get("app_settings", {}).get("wake_word", "крест")

# Динамическое имя хозяина (читается из config.json, создаваемого GUI)
OWNER_NAME = get_owner_name()

# Строгое требование фразы обращения перед командой
REQUIRE_WAKE_WORD = True

# Время ожидания команды после произнесения имени (в секундах)
ACTIVE_TIMEOUT = 6.0

# Порог чувствительности FuzzyWuzzy для распознавания имени (0-100)
FUZZY_THRESHOLD = 75

# ==========================================
# 3. Настройки звука, STT и TTS (Silero/Vosk)
# ==========================================
SAMPLE_RATE = 16000
SAMPLERATE = SAMPLE_RATE  # Псевдоним для совместимости

# Настройки громкости и активации озвучки
VOICE_ENABLED = _user_cfg.get("app_settings", {}).get("voice_enabled", True)
VOICE_VOLUME = _user_cfg.get("app_settings", {}).get("voice_volume", 80)

# Параметры синтеза речи Silero TTS
TTS_SPEAKER = "kseniya"  # Доступные дикторы: kseniya, baya, xenia, aidar, eugene
TTS_SAMPLE_RATE = 48000
TTS_THREADS = 4  # Количество потоков для PyTorch

# ==========================================
# 4. Настройки визуального индикатора (Оверлея)
# ==========================================
INDICATOR_ENABLED = True
INDICATOR_WIDTH = 50
INDICATOR_HEIGHT = 65

# Аметистовая цветовая палитра по умолчанию
COLOR_GLOW = "#2d1b38"  # Теневой ореол
COLOR_MAIN = "#9b72b0"  # Основная линия
COLOR_CORE = "#f4effa"  # Ядро

# ==========================================
# 5. Настройки Dota 2 Гида и GSI Интеграции
# ==========================================
_dota_cfg = _user_cfg.get("dota_guide", {})
DOTA_PROCESS_NAME = _dota_cfg.get("process_name", "dota2.exe")
DOTA2_PROCESS_NAME = DOTA_PROCESS_NAME  # Псевдоним для совместимости

DOTA_GSI_PORT = _dota_cfg.get("gsi_port", 3000)
DOTA_GUIDE_ENABLED = _dota_cfg.get("enabled", True)

# Время предупреждения о событиях (за X секунд до спавна)
WARNING_LEAD_TIME = _dota_cfg.get("warning_lead_time", 30)

# Интервалы ключевых игровых событий (в секундах)
BOUNTY_RUNE_INTERVAL = 180  # Руны богатства (каждые 3 минуты)
WISDOM_RUNE_INTERVAL = 420  # Руны мудрости (каждые 7 минут)
LOTUS_INTERVAL = 180  # Лотусы (каждые 3 минуты)

# ==========================================
# 6. Настройки API Ключей и Внешних Сервисов
# ==========================================
_api_cfg = _user_cfg.get("api_keys", {})
STEAM_API_KEY = _api_cfg.get("steam_api_key", "")
STEAM_ID = _api_cfg.get("steam_id", "")
DISCORD_TOKEN = _api_cfg.get("discord_token", "")
DISCORD_CHANNEL_ID = _api_cfg.get("discord_channel_id", "")
TELEGRAM_API_ID = _api_cfg.get("telegram_api_id", "")
TELEGRAM_API_HASH = _api_cfg.get("telegram_api_hash", "")
TELEGRAM_PHONE = _api_cfg.get("telegram_phone", "")

# ==========================================
# 7. Статус Лицензии и Переключатели Модулей
# ==========================================
IS_PREMIUM = _user_cfg.get("license", {}).get("is_premium", False)
MODULES = _user_cfg.get(
    "modules",
    {
        "media_skills": True,
        "windows_skills": True,
        "dota_guide": True,
        "telegram_bot": True,
        "discord_bot": True,
        "steam_integration": True,
    },
)

# ==========================================
# 8. Системные Настройки (Трей и Автозапуск)
# ==========================================
_app_cfg = _user_cfg.get("app_settings", {})
AUTOSTART = _app_cfg.get("autostart", False)
MINIMIZE_TO_TRAY = _app_cfg.get("minimize_to_tray", True)
CLOSE_TO_TRAY = _app_cfg.get("close_to_tray", True)

# ==========================================
# 9. Отладка и логирование
# ==========================================
DEBUG_MODE = False
LOG_LEVEL = "INFO"

# Compatibility with the terminal bot and Discord bridge.
KREST_BOT_TOKEN = os.getenv("KREST_BOT_TOKEN", _api_cfg.get("krest_bot_token", ""))
MY_ID = int(_api_cfg.get("telegram_owner_id") or 0)
MY_CHANNEL_ID = _api_cfg.get("telegram_channel_id", "")
DS_USER_TOKEN = os.getenv("DISCORD_TOKEN", DISCORD_TOKEN)
