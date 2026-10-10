"""Bounded, cached public data adapters. No account tokens or gameplay automation."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import requests
from storage import atomic_write_json

CATALOGS = (
    "Warframes",
    "Primary",
    "Secondary",
    "Melee",
    "Sentinels",
    "Archwing",
    "Resources",
    "Misc",
    "Relics",
    "Mods",
)
ITEMS_BASE = "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/"
WORLD_BASE = "https://api.warframestat.us/"
MARKET_BASE = "https://api.warframe.market/v2/"
CYCLES = {
    "cetusCycle": "Равнины Эйдолона",
    "vallisCycle": "Долина Сфер",
    "cambionCycle": "Камбионский Дрейф",
    "zarimanCycle": "Зариман",
}
STATES = {
    "day": "день",
    "night": "ночь",
    "warm": "тепло",
    "cold": "холод",
    "fass": "Фасс",
    "vome": "Воум",
    "grineer": "Гринир",
    "corpus": "Корпус",
}


def timestamp(value):
    try:
        if isinstance(value, (int, float)):
            return float(value) / 1000 if value > 10**11 else float(value)
        return (
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp()
        )
    except (ValueError, TypeError, OverflowError):
        return 0


def local_time(value):
    stamp = timestamp(value)
    return datetime.fromtimestamp(stamp).strftime("%d.%m %H:%M") if stamp else "время не указано"


def remaining(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return (
        f"{hours} ч {minutes} мин" if hours else f"{minutes} мин" if minutes else f"{seconds} сек"
    )


class DataCache:
    def __init__(self, folder, transport=None, clock=time.time):
        self.folder = Path(folder)
        self.clock = clock
        self.transport = transport or requests.get
        self._guard = threading.RLock()
        self._locks = {}
        self.retry = {}
        self.errors = {}
        self._market_lock = threading.Lock()
        self._last_market = 0

    def read(self, key):
        path = self.folder / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        try:
            if path.stat().st_size > 24 * 1024**2:
                raise ValueError("Кэш слишком большой")
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("updated"), (int, float))
                or "data" not in data
            ):
                return None
            return data
        except (OSError, ValueError):
            return None

    def get(self, key, url, ttl=120, force=False, validator=None, limit=20 * 1024**2, headers=None):
        if not url.startswith((ITEMS_BASE, WORLD_BASE, MARKET_BASE)):
            raise ValueError("Неизвестный источник Warframe")
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            saved = self.read(key)
            now = self.clock()
            if saved and validator and not validator(saved["data"]):
                saved = None
            if saved and not force and 0 <= now - saved["updated"] < ttl:
                return dict(
                    saved,
                    stale=bool(self.errors.get(key)),
                    error=self.errors.get(key, ""),
                    source=url,
                )
            if now < self.retry.get(key, 0):
                if saved:
                    return dict(
                        saved,
                        stale=bool(self.errors.get(key)) or not 0 <= now - saved["updated"] < ttl,
                        error=self.errors.get(key, ""),
                        source=url,
                    )
                raise RuntimeError(
                    self.errors.get(key, "Источник временно недоступен; повторный запрос отложен")
                )
            try:
                if url.startswith(MARKET_BASE):
                    with self._market_lock:
                        delay = 0.4 - (time.monotonic() - self._last_market)
                        if delay > 0:
                            time.sleep(delay)
                        self._last_market = time.monotonic()
                with self.transport(
                    url,
                    timeout=(4, 10),
                    stream=True,
                    headers={
                        "User-Agent": "Krest/1.0 (personal Warframe companion)",
                        "Accept": "application/json",
                        "Language": "en",
                        **(headers or {}),
                    },
                ) as response:
                    if response.status_code == 429:
                        try:
                            delay = max(
                                30, min(600, int(response.headers.get("Retry-After", "120")))
                            )
                        except ValueError:
                            delay = 120
                        self.retry[key] = now + delay
                    response.raise_for_status()
                    chunks = []
                    total = 0
                    start = time.monotonic()
                    for chunk in response.iter_content(65536):
                        total += len(chunk)
                        if total > limit or time.monotonic() - start > 25:
                            raise ValueError("Ответ источника слишком большой или медленный")
                        chunks.append(chunk)
                    value = json.loads(b"".join(chunks))
                if validator and not validator(value):
                    raise ValueError("Источник вернул неожиданный формат данных")
                if not isinstance(value, (dict, list)):
                    raise ValueError("Ожидался JSON-объект или список")
                saved = {"updated": self.clock(), "data": value}
                self.folder.mkdir(parents=True, exist_ok=True)
                atomic_write_json(
                    self.folder / (hashlib.sha256(key.encode()).hexdigest() + ".json"), saved
                )
                self.errors.pop(key, None)
                self.retry[key] = self.clock() + 15
                return dict(saved, stale=False, error="", source=url)
            except (requests.RequestException, OSError, ValueError) as exc:
                message = "Нет связи с источником: " + str(exc)[:350]
                self.errors[key] = message
                self.retry[key] = max(self.retry.get(key, 0), now + 60)
                if saved:
                    return dict(saved, stale=True, error=message, source=url)
                raise RuntimeError(message) from exc


def cycle_rows(world, now=None):
    now = time.time() if now is None else now
    rows = []
    for key, name in CYCLES.items():
        value = world.get(key)
        if not isinstance(value, dict):
            continue
        expiry = timestamp(value.get("expiry"))
        state = str(value.get("state", value.get("active", "")))
        if not state:
            state = (
                ("day" if value["isDay"] else "night")
                if "isDay" in value
                else ("warm" if value["isWarm"] else "cold")
                if "isWarm" in value
                else ""
            )
        rows.append(
            {
                "id": key,
                "name": name,
                "state": STATES.get(state.casefold(), state or "не указано"),
                "raw_state": state.casefold(),
                "expiry": expiry,
                "remaining": remaining(expiry - now) if expiry > now else "Нужно обновить",
                "expired": expiry <= now,
            }
        )
    return rows


def event_rows(world, now=None):
    now = time.time() if now is None else now
    rows = []
    for group, title in [
        ("fissures", "Разлом"),
        ("invasions", "Вторжение"),
        ("alerts", "Сигнал тревоги"),
        ("events", "Событие"),
    ]:
        for item in world.get(group, []) if isinstance(world.get(group), list) else []:
            if not isinstance(item, dict) or item.get("completed") or item.get("expired"):
                continue
            ends = timestamp(item.get("expiry"))
            if ends and ends <= now:
                continue
            mission = item.get("mission", {}) or {}
            reward = mission.get("reward", {}) or item.get("reward", {}) or {}
            rewards = []
            for part in (reward, item.get("attackerReward", {}), item.get("defenderReward", {})):
                if isinstance(part, dict):
                    text = part.get("asString") or ", ".join(str(v) for v in part.get("items", []))
                    if text:
                        rewards.append(text)
            label = " · ".join(
                str(v)
                for v in [
                    item.get("tier"),
                    item.get("node") or mission.get("node"),
                    item.get("missionType") or mission.get("type"),
                    item.get("description"),
                ]
                if v
            )
            if item.get("isHard"):
                label += " · Стальной путь"
            rows.append(
                {
                    "id": str(item.get("id") or hashlib.sha256(label.encode()).hexdigest()),
                    "kind": title,
                    "name": label or title,
                    "reward": "; ".join(rewards),
                    "expiry": ends,
                }
            )
    for group, title in [("sortie", "Вылазка"), ("archonHunt", "Охота на Архонта")]:
        item = world.get(group)
        if isinstance(item, dict) and timestamp(item.get("expiry")) > now:
            missions = item.get("variants", item.get("missions", []))
            label = "; ".join(
                str(m.get("node", "")) + " " + str(m.get("missionType", m.get("type", "")))
                for m in missions
                if isinstance(m, dict)
            )
            rows.append(
                {
                    "id": str(item.get("id", group)),
                    "kind": title,
                    "name": label,
                    "reward": str(item.get("boss", "")),
                    "expiry": timestamp(item.get("expiry")),
                }
            )
    nightwave = world.get("nightwave")
    for item in nightwave.get("activeChallenges", []) if isinstance(nightwave, dict) else []:
        if not isinstance(item, dict):
            continue
        expiry = timestamp(item.get("expiry"))
        if expiry and expiry <= now:
            continue
        rows.append(
            {
                "id": str(item.get("id", item.get("title", ""))),
                "kind": "Ночная Волна",
                "name": str(item.get("title", "")) + ": " + str(item.get("desc", "")),
                "reward": str(item.get("reputation", "")) + " репутации",
                "expiry": expiry,
            }
        )
    return rows[:300]


def world_briefing(envelope, goals=()):
    world = envelope["data"]
    now = time.time()
    lines = []
    lines.append(
        ("СОХРАНЁННЫЕ ДАННЫЕ, НЕ АКТУАЛЬНАЯ СВОДКА. " if envelope["stale"] else "")
        + "Обновлено "
        + local_time(envelope["updated"])
    )
    lines.extend(
        f"{r['name']}: {r['state']}, до смены — {r['remaining']}." for r in cycle_rows(world, now)
    )
    baro = world.get("voidTrader") or {}
    if baro:
        start, end = timestamp(baro.get("activation")), timestamp(baro.get("expiry"))
        lines.append(
            ("Баро сейчас: " + str(baro.get("location", "")) + "; уходит " + local_time(end))
            if start <= now < end
            else "Баро: следующее указанное прибытие " + local_time(start)
        )
    events = event_rows(world, now)
    lines.append("Доступные активности: " + str(len(events)) + ".")
    lines.extend(
        r["kind"] + ": " + r["name"] for r in events if r["kind"] in ("Вылазка", "Охота на Архонта")
    )
    pending = [r["name"] for r in goals if not r.get("done")]
    if pending:
        lines.append("Ваши цели: " + ", ".join(pending[:5]) + ".")
    if envelope["error"]:
        lines.append(envelope["error"])
    return "\n".join(lines)


ALIASES = {
    "висп": "Wisp",
    "висп прайм": "Wisp Prime",
    "некрос": "Nekros",
    "некрос прайм": "Nekros Prime",
    "экскалибур": "Excalibur",
    "вольт": "Volt",
    "вольт прайм": "Volt Prime",
    "миса": "Mesa",
    "миса прайм": "Mesa Prime",
    "рино": "Rhino",
    "рино прайм": "Rhino Prime",
    "форма": "Forma",
    "нейроды": "Neurodes",
    "нейронные датчики": "Neural Sensors",
    "клетки орокин": "Orokin Cell",
    "ячейки орокин": "Orokin Cell",
    "аргон": "Argon Crystal",
    "кристалл аргона": "Argon Crystal",
    "пластиды": "Plastids",
    "полимеры": "Polymer Bundle",
    "феррит": "Ferrite",
    "наноспоры": "Nano Spores",
    "криотик": "Cryotic",
    "эндo": "Endo",
    "эндо": "Endo",
    "мезо": "Meso",
    "нео": "Neo",
    "лит": "Lith",
    "акси": "Axi",
}


def item_query(text, aliases=None):
    text = str(text).strip()
    merged = {**ALIASES, **(aliases or {})}
    return merged.get(text.casefold(), text).casefold()


class ItemCatalog:
    def __init__(self, cache):
        self.cache = cache
        self.groups = {}
        self.lock = threading.RLock()
        self.info = {}

    def load(self, category, force=False):
        if category not in CATALOGS:
            raise ValueError("Неизвестный справочник")
        with self.lock:
            if (
                not force
                and category in self.groups
                and time.time() - self.info[category]["updated"] < 86400
            ):
                return self.groups[category]
            result = self.cache.get(
                "items-" + category,
                ITEMS_BASE + category + ".json",
                ttl=86400,
                force=force,
                validator=lambda d: isinstance(d, list),
            )
            keys = {
                "name",
                "uniqueName",
                "description",
                "category",
                "type",
                "components",
                "buildPrice",
                "buildTime",
                "buildQuantity",
                "locations",
                "rewards",
                "vaulted",
                "rarity",
                "fusionLimit",
                "baseDrain",
                "polarity",
                "levelStats",
                "wikiaUrl",
                "masteryReq",
                "tradable",
                "drops",
                "marketInfo",
            }
            self.groups[category] = [
                {k: v for k, v in row.items() if k in keys}
                for row in result["data"]
                if isinstance(row, dict) and isinstance(row.get("name"), str)
            ]
            self.info[category] = {k: result[k] for k in ("updated", "stale", "error", "source")}
            return self.groups[category]

    def search(self, query, category="Warframes", aliases=None):
        term = item_query(query, aliases)
        if not term:
            raise ValueError("Введите название предмета")
        rows = self.load(category)
        selected = [row for row in rows if term in row["name"].casefold()]
        selected.sort(key=lambda row: (row["name"].casefold() != term, row["name"]))
        return selected[:80]

    def exact(self, query, category="Warframes", aliases=None):
        term = item_query(query, aliases)
        matches = [r for r in self.search(query, category, aliases) if r["name"].casefold() == term]
        if len(matches) > 1:
            recipes = [r for r in matches if r.get("components")]
            if len(recipes) == 1:
                return recipes[0]
        if len(matches) != 1:
            raise ValueError("Нужно точное название предмета. Выберите его в справочнике.")
        return matches[0]

    def relics_for(self, query, aliases=None):
        term = item_query(query, aliases)
        if not term:
            raise ValueError("Введите название награды или реликвии")
        rows = []
        for relic in self.load("Relics"):
            for reward in relic.get("rewards", []):
                item = reward.get("item", {})
                name = item.get("name", "") if isinstance(item, dict) else str(item)
                if term in name.casefold() or term in relic["name"].casefold():
                    rows.append(
                        {
                            "relic": relic["name"],
                            "item": name,
                            "chance": reward.get("chance"),
                            "rarity": reward.get("rarity", ""),
                            "vaulted": relic.get("vaulted"),
                            "locations": relic.get("locations", []),
                        }
                    )
        return rows[:200]


def normalize_orders(data, online_only=True, rank=None):
    source = data.get("data", data.get("payload", {}))
    if isinstance(source, dict):
        source = source.get("orders", [])
    if not isinstance(source, list):
        raise ValueError("Рынок вернул неизвестный формат предложений")
    rows = []
    for item in source:
        if not isinstance(item, dict) or not isinstance(item.get("user"), dict):
            continue
        user = item.get("user", {})
        price = item.get("platinum")
        quantity = item.get("quantity", 0)
        status = user.get("status", "offline")
        if (
            item.get("type", item.get("order_type")) != "sell"
            or item.get("visible") is False
            or online_only
            and status not in ("ingame", "online")
        ):
            continue
        if (
            type(price) is not int
            or not 0 < price < 10**7
            or type(quantity) is not int
            or quantity <= 0
        ):
            continue
        if rank is not None and item.get("rank", item.get("mod_rank")) != rank:
            continue
        seller = user.get("ingameName", user.get("ingame_name", ""))
        if not isinstance(seller, str) or not re.fullmatch(r"[\w.\-]{1,80}", seller):
            continue
        rows.append(
            {
                "id": str(
                    item.get("id")
                    or hashlib.sha256(
                        (seller + str(price) + str(item.get("rank"))).encode()
                    ).hexdigest()
                ),
                "seller": seller,
                "price": int(price),
                "quantity": int(quantity),
                "status": status,
                "rank": item.get("rank", item.get("mod_rank")),
            }
        )
    return sorted(rows, key=lambda r: (r["price"], r["seller"]))[:50]


def market_items(data):
    source = data.get("data", data.get("payload", {}))
    if isinstance(source, dict):
        source = source.get("items", [])
    if not isinstance(source, list):
        raise ValueError("Некорректный список предметов рынка")
    result = []
    for item in source:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug", item.get("url_name", ""))
        name = (item.get("i18n", {}).get("en", {}) or {}).get("name", item.get("item_name", slug))
        ru = (item.get("i18n", {}).get("ru", {}) or {}).get("name", "")
        if re.fullmatch("[a-z0-9_]{1,160}", str(slug)):
            result.append({"slug": slug, "name": name, "ru": ru})
    return result
