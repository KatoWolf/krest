"""Deterministic planning and reference calculations for a personal Warframe notebook."""

from datetime import datetime, timedelta, timezone
import time
from urllib.parse import quote
from warframe_api import item_query

RESOURCE_GUIDE = {
    "neurodes": (
        "Земля, Луа, Деймос, Эрида",
        "Ищите нейроптические массы и ресурсные контейнеры. На Земле удобно совмещать сбор с обычными миссиями.",
    ),
    "neural sensors": (
        "Юпитер, Крепость Кувы",
        "На Юпитере сбор можно совмещать с миссиями и боем с Аладом V.",
    ),
    "orokin cell": (
        "Церера, Сатурн, Деймос",
        "Собирайте ресурсные контейнеры; выживание и оборона позволяют одновременно прокачивать снаряжение.",
    ),
    "argon crystal": (
        "Бездна",
        "Разрушайте контейнеры и залежи аргона. Ресурс распадается со временем: собирайте незадолго до изготовления.",
    ),
    "plastids": (
        "Фобос, Сатурн, Уран, Плутон, Эрида",
        "Тёмные сектора и длительные миссии удобны для сбора; проверьте доступные вам узлы.",
    ),
    "polymer bundle": (
        "Меркурий, Венера, Уран",
        "Сбор удобно совмещать с выживанием и другими ресурсными целями.",
    ),
    "ferrite": ("Земля, Меркурий, Нептун, Бездна", "Обычные контейнеры, залежи и противники."),
    "nano spores": (
        "Сатурн, Нептун, Эрида, Деймос",
        "Частый ресурс; длительные миссии позволяют собрать его вместе с редкими ресурсами.",
    ),
    "cryotic": (
        "Миссии раскопок",
        "Награда связана с работой экскаваторов. Выберите доступный узел и поддерживайте питание.",
    ),
    "endo": (
        "Скульптуры Аятан, разбор лишних модов, награды миссий",
        "Не разбирайте нужные моды. Заполненные звёздами скульптуры дают больше эндо.",
    ),
}


def resource_advice(query, aliases=None):
    key = item_query(query, aliases)
    guide = RESOURCE_GUIDE.get(key)
    if guide:
        return f"{query}: {guide[0]}.\n{guide[1]}\nЭто справочные места, без обещания количества ресурса за минуту."
    return ""


def reset_start(task, now=None):
    now = time.time() if now is None else now
    value = datetime.fromtimestamp(now, timezone.utc) - timedelta(
        hours=int(task.get("reset_hour", 0))
    )
    start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    if task.get("period") == "weekly":
        start -= timedelta(days=(start.weekday() - int(task.get("reset_weekday", 0))) % 7)
    return (start + timedelta(hours=int(task.get("reset_hour", 0)))).timestamp()


def task_done(task, now=None):
    if task.get("period") == "once":
        return bool(task.get("done"))
    return float(task.get("completed_at", 0)) >= reset_start(task, now)


def next_reset(task, now=None):
    if task.get("period") == "once":
        return 0
    return reset_start(task, now) + (7 if task.get("period") == "weekly" else 1) * 86400


def session_plan(state, minutes):
    minutes = int(minutes)
    if not 5 <= minutes <= 480:
        raise ValueError("Укажите от 5 до 480 минут")
    candidates = []
    for group in ("tasks", "goals"):
        for row in state[group]:
            if task_done(row) if group == "tasks" else row.get("done"):
                continue
            duration = int(row.get("minutes", 15)) or 15
            candidates.append(dict(row, minutes=duration, section=group))
    candidates.sort(key=lambda r: (-int(r.get("priority", 2)), r["minutes"], r.get("created", 0)))
    selected = []
    left = minutes
    for row in candidates:
        if row["minutes"] <= left:
            selected.append(row)
            left -= row["minutes"]
    lines = [f"План на {minutes} минут. Оценки длительности берутся из ваших записей."]
    lines.extend(
        f"{i + 1}. {row['name']} — около {row['minutes']} мин" for i, row in enumerate(selected)
    )
    if not selected:
        lines.append(
            "Нет подходящих по времени незавершённых целей. Добавьте цель или увеличьте время."
        )
    else:
        lines.append(
            f"Остаётся примерно {left} мин на загрузки, выбор снаряжения и завершение миссии."
        )
    return {"text": "\n".join(lines), "rows": selected, "remaining": left}


def trace_cost(tier, count, owned=0):
    tiers = {
        "intact": 0,
        "exceptional": 25,
        "flawless": 50,
        "radiant": 100,
        "нетронутая": 0,
        "необычная": 25,
        "безупречная": 50,
        "сияющая": 100,
    }
    key = str(tier).casefold().strip()
    if key not in tiers:
        raise ValueError("Уровни: intact, exceptional, flawless, radiant")
    count = int(count)
    owned = int(owned)
    if not 1 <= count <= 1000 or not 0 <= owned <= 100000:
        raise ValueError("Проверьте количество реликвий и следов")
    total = tiers[key] * count
    return {
        "total": total,
        "missing": max(0, total - owned),
        "text": f"Для {count} нетронутых реликвий → {tier} нужно {total} Следов Бездны. Не хватает: {max(0, total - owned)}.",
    }


def mod_upgrade_cost(rarity, current, target):
    scale = {"common": 1, "uncommon": 2, "rare": 3, "legendary": 4}.get(str(rarity).casefold())
    current, target = int(current), int(target)
    if scale is None or not 0 <= current <= target <= 10:
        raise ValueError("Неизвестная редкость или ранг мода")
    steps = 2**target - 2**current
    return {"endo": 10 * scale * steps, "credits": 483 * scale * steps}


def parse_mods(text):
    result = {}
    for line in str(text).splitlines():
        if not line.strip():
            continue
        name, sep, rank = line.rpartition(":")
        if not sep:
            name, rank = line, "0"
        name = name.strip()
        if not name or not rank.strip().isdigit():
            raise ValueError("Каждый мод: название:ранг, например Vitality:10")
        if name in result:
            raise ValueError("Мод указан дважды: " + name)
        value = int(rank)
        if value > 10:
            raise ValueError("Ранг мода: 0–10")
        result[name] = value
    if len(result) > 20:
        raise ValueError("В одном билде не более 20 модов")
    return result


def build_report(build, state, mods):
    by_name = {row["name"].casefold(): row for row in mods}
    owned = {k.casefold(): v for k, v in state["mod_ranks"].items()}
    missing = []
    unknown = []
    endo = credits = 0
    for name, rank in build.get("mods", {}).items():
        current = owned.get(name.casefold())
        if current is None:
            missing.append(name)
            current = 0
        row = by_name.get(name.casefold())
        if not row or not isinstance(row.get("fusionLimit"), int) or rank > row["fusionLimit"]:
            unknown.append(name)
            continue
        if current >= rank:
            continue
        try:
            cost = mod_upgrade_cost(row.get("rarity"), current, rank)
        except ValueError:
            unknown.append(name)
            continue
        endo += cost["endo"]
        credits += cost["credits"]
    text = f"{build['name']}\nНе отмечены в коллекции: {', '.join(missing) or 'нет'}.\nРасчёт усиления известных обычных модов: {endo} эндо и {credits} кредитов. Для отсутствующих модов расчёт начинается с ранга 0; приобретение не включено."
    if unknown:
        text += "\nНе рассчитаны (неизвестный мод, особая редкость или ранг): " + ", ".join(unknown)
    return {"text": text, "missing": missing, "unknown": unknown, "endo": endo, "credits": credits}


def wiki_url(query):
    return "https://wiki.warframe.com/w/Special:Search?search=" + quote(str(query))
