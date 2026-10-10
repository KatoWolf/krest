"""Steam voice/text queries use the monitor's cache and never trade items."""

from datetime import datetime
from pathlib import Path
from skill_manager import skill
from steam_service import read_cache, cached_catalog


def cache_for(state):
    path = (
        state.filepath.parent if state.filepath else Path(__file__).resolve().parents[1]
    ) / "steam_state.json"
    cache = read_cache(path)
    if cache.get("account", "") != str(
        state.config_getter().get("api_keys", {}).get("steam_id", "")
    ):
        return {}
    return cache


@skill(
    keywords=["библиотека стим", "все мои игры", "моя библиотека steam"],
    description="Игры аккаунта Steam и установленные игры",
    category="Steam & Gaming",
)
def library(state):
    games = cached_catalog(state)
    if not games:
        return "Библиотека пока пуста. Откройте Steam / CS2 и запустите монитор."
    return (
        f"Игр в библиотеке: {len(games)}. "
        + "; ".join(g["name"] for g in games[:40])
        + (". Остальные игры — в меню Steam." if len(games) > 40 else "")
    )


@skill(
    keywords=[
        "инвентарь кс",
        "инвентарь cs2",
        "ограничения обмена",
        "сколько предметов заблокировано",
    ],
    description="Предметы CS2 и доступность обмена по последней проверке Steam",
    category="Steam & Gaming",
)
def inventory(state):
    cache = cache_for(state)
    data = cache.get("inventories", {}).get("730:2", {})
    if not data:
        return "Нет данных CS2. Запустите монитор Steam; инвентарь должен быть открытым."
    counts = data["summary"]
    checked = datetime.fromtimestamp(data["updated"]).strftime("%d.%m %H:%M")
    text = f"По проверке {checked}: предметов {counts['total']}, обмен доступен для {counts['tradable']}, недоступен для {counts['locked']}, статус неизвестен для {counts['unknown']}."
    if cache.get("errors", {}).get("730:2"):
        text += " Последнее обновление не удалось; это сохранённые данные."
    return text


@skill(
    keywords=["предложения обмена", "мои трейды"],
    description="Число входящих и исходящих предложений обмена Steam",
    category="Steam & Gaming",
)
def offers(state):
    cache = cache_for(state)
    if "offers_updated" not in cache:
        return "Предложения обмена ещё не получены. Проверьте ключ Steam в меню."
    rows = cache.get("offers", [])
    return f"По последней проверке: входящих предложений {sum(v['direction'] == 'in' for v in rows)}, исходящих {sum(v['direction'] == 'out' for v in rows)}. Проверьте актуальность на странице Steam."
