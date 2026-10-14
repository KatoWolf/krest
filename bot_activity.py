"""Fresh shared activity and five-minute presence rotation for account bridges."""

import time
from pathlib import Path
from storage import read_json

STATUS_BANK = {
    "friendly": (
        "Крест рядом",
        "На связи, когда есть минутка",
        "Ловлю хорошие новости",
        "Чат под присмотром Крест",
    ),
    "concise": ("Занят делами", "Отвечаю по возможности", "На связи", "Без лишнего шума"),
    "lively": (
        "На своей волне",
        "Идеи в движении",
        "Крест держит связь",
        "Между делом и приключением",
    ),
    "calm": ("Спокойный ритм", "Всему своё время", "Небольшая пауза", "Берегу внимание"),
    "tactical": (
        "Оператор на месте",
        "Канал связи открыт",
        "Крест на дежурстве",
        "Задачи под контролем",
    ),
    "ironic": (
        "Загружаю желание отвечать",
        "Статус есть, свободное время ищем",
        "Крест работает, я стараюсь",
        "Всё по плану. Почти",
    ),
    "gothic": (
        "Крест хранит тишину",
        "Свет у порога",
        "На границе миров",
        "Хранительница на посту",
    ),
}
GAME_STATUS_BANK = {
    "friendly": (
        "В игре · Крест рядом",
        "Играю, отвечу в паузе",
        "Ещё одна партия",
        "Чат доверен Крест",
    ),
    "concise": ("В игре", "Партия идёт", "Ответ после игры", "Игровой режим"),
    "lively": (
        "Навстречу приключениям",
        "Ловлю победу в игре",
        "В игре, держите кулачки",
        "Ещё один заход!",
    ),
    "calm": (
        "Играю в своём ритме",
        "В игре · без спешки",
        "Отдых в другом мире",
        "Сосредоточен на игре",
    ),
    "tactical": (
        "Оператор на миссии",
        "Задание в процессе",
        "Боевой канал активен",
        "Крест прикрывает связь",
    ),
    "ironic": (
        "Спасаю мир. Или лут",
        "В игре · это последний заход",
        "Пауза недоступна, Крест доступна",
        "Фармлю терпение",
    ),
    "gothic": (
        "В игре · охота началась",
        "За порогом другого мира",
        "Крест у костра, я в бою",
        "По следам новой добычи",
    ),
}


def local_activity(folder, now=None):
    now = time.time() if now is None else now
    try:
        value = read_json(Path(folder) / "current_status.json")
        if isinstance(value, dict) and 0 <= now - float(value.get("updated_at", 0)) < 60:
            return value
    except (OSError, ValueError, TypeError):
        pass
    return {}


def reply_options(config, service):
    result = dict(config["auto_replies"])
    # Both switches refer to deliberately sharing activity with this service.
    result["share_activity"] = bool(
        result.get("share_activity") or config[service].get("follow_activity")
    )
    return result


def status_text(config, service, activity, now=None):
    settings = config[service]
    personality = config["profile"]["personality"]
    playing = settings.get("follow_activity") and activity.get("is_playing")
    fallback = settings.get("status_text", settings.get("bio_text", "Крест рядом"))
    if not settings.get("rotate_status", True):
        return "В игре · Крест рядом" if playing else fallback
    custom = settings.get("game_statuses" if playing else "statuses", "")
    values = [v.strip() for v in custom.splitlines() if v.strip()]
    bank = GAME_STATUS_BANK if playing else STATUS_BANK
    values = values or list(bank.get(personality, bank["friendly"]))
    index = int((time.time() if now is None else now) // 300) % len(values)
    limit = 70 if service == "telegram_user" else 128
    return values[index][:limit]


def chat_muted(folder, service, chat):
    # Read the same local preferences as the notification menu; no remote history.
    from notification_center import NotificationCenter

    path = Path(folder) / "inbox.sqlite3"
    return path.exists() and NotificationCenter(path).muted(service, chat)
