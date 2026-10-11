"""Steam read-only data service. AppIDs are launch IDs, never activation keys."""

from storage import read_json


from datetime import datetime, timezone
import copy
import html
import json
import os
from pathlib import Path
import re
import time

import requests
from storage import atomic_write_json


class SteamError(RuntimeError):
    pass


def read_cache(path):
    try:
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def local_games(install_path=""):
    from skills.webo import get_steam_install_path

    roots = [Path(install_path).expanduser()] if install_path else []
    detected = get_steam_install_path()
    if detected:
        roots.append(Path(detected))
    roots += [
        Path.home() / ".steam/steam",
        Path.home() / ".local/share/Steam",
        Path.home() / "Library/Application Support/Steam",
    ]
    libraries = set()
    for root in roots:
        if root.is_dir():
            libraries.add(root)
            try:
                content = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8")
                libraries.update(
                    Path(value.replace("\\\\", "\\"))
                    for value in re.findall(r'"path"\s+"([^"]+)"', content, re.I)
                )
            except OSError:
                pass
    games = {}
    for root in libraries:
        for manifest in (root / "steamapps").glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
                values = dict(re.findall(r'"(\w+)"\s+"([^"\n]*)"', text))
                appid = values.get("appid", "")
                if not appid.isdigit() or not values.get("name"):
                    continue
                flags = int(values.get("StateFlags", "0"))
                games[appid] = {
                    "appid": appid,
                    "name": values["name"],
                    "installed": bool(flags & 4),
                    "library": str(root),
                    **({"install_dir": values["installdir"]} if values.get("installdir") else {}),
                }
            except (ValueError, OSError):
                continue
    return list(games.values())


def merge_games(owned, installed):
    merged = {
        str(g["appid"]): {**g, "appid": str(g["appid"]), "installed": False}
        for g in owned
        if str(g.get("appid", "")).isdigit()
    }
    for game in installed:
        merged[game["appid"]] = {**merged.get(game["appid"], {}), **game}
    return sorted(merged.values(), key=lambda g: g.get("name", "").casefold())


def plain(value):
    return html.unescape(re.sub(r"<[^>]*>", "", str(value)))[:1000]


def unlock_time(description):
    # Steam's restriction-day counts are NOT an asset's unlock timestamp.
    for line in description.get("owner_descriptions", []):
        text = plain(line.get("value", ""))
        match = re.search(
            r"(?:Tradable|Tradeable) After (\w+) (\d{1,2}), (\d{4}) \((\d{1,2}):(\d{2}):(\d{2})\) GMT",
            text,
            re.I,
        )
        if match:
            month, day, year, hour, minute, second = match.groups()
            try:
                month_num = [
                    "jan",
                    "feb",
                    "mar",
                    "apr",
                    "may",
                    "jun",
                    "jul",
                    "aug",
                    "sep",
                    "oct",
                    "nov",
                    "dec",
                ].index(month[:3].lower()) + 1
                return datetime(
                    int(year),
                    month_num,
                    int(day),
                    int(hour),
                    int(minute),
                    int(second),
                    tzinfo=timezone.utc,
                ).timestamp()
            except ValueError:
                pass
    return None


class SteamClient:
    def __init__(self, api_key, steam_id, session=None):
        self.api_key, self.steam_id = api_key, str(steam_id)
        self.session = session or requests.Session()
        if not re.fullmatch(r"\d{17}", self.steam_id):
            raise SteamError("Укажите SteamID64 — 17 цифр из адреса профиля")

    def get(self, url, params=None):
        try:
            response = self.session.get(url, params=params, timeout=(5, 25))
            if response.status_code == 429:
                raise SteamError(
                    "Steam ограничил частоту запросов. Повтор — при следующей плановой проверке."
                )
            if response.status_code in (401, 403):
                raise SteamError(
                    "Steam отказал в доступе: проверьте ключ и приватность профиля/инвентаря"
                )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("object expected")
            return data
        except (requests.RequestException, ValueError) as exc:
            raise SteamError(
                "Steam недоступен или вернул некорректный ответ; прежние данные сохранены"
            ) from exc

    def api(self, method, **params):
        if not self.api_key:
            raise SteamError("Для библиотеки аккаунта и обменов нужен Steam Web API Key")
        return self.get("https://api.steampowered.com/" + method, {"key": self.api_key, **params})

    def owned_games(self):
        data = self.api(
            "IPlayerService/GetOwnedGames/v1/",
            steamid=self.steam_id,
            include_appinfo=1,
            include_played_free_games=1,
        ).get("response", {})
        if "game_count" not in data:
            raise SteamError(
                "Библиотека закрыта или недоступна. Разрешите видимость сведений об играх в Steam."
            )
        games = data.get("games", [])
        if not isinstance(games, list) or len(games) != int(data["game_count"]):
            raise SteamError("Получена неполная библиотека; прежний список сохранён")
        return [
            {
                "appid": str(g["appid"]),
                "name": str(g.get("name") or g["appid"]),
                "playtime_minutes": g.get("playtime_forever", 0),
            }
            for g in games
        ]

    def inventory(self, appid, contextid):
        assets, descriptions, seen_pages = {}, {}, set()
        cursor = None
        for _ in range(100):
            params = {"l": "english", "count": 2000}
            if cursor:
                params["start_assetid"] = cursor
            data = self.get(
                f"https://steamcommunity.com/inventory/{self.steam_id}/{appid}/{contextid}", params
            )
            if data.get("success") != 1:
                raise SteamError("Инвентарь недоступен или скрыт настройками приватности")
            if not isinstance(data.get("assets", []), list) or not isinstance(
                data.get("descriptions", []), list
            ):
                raise SteamError("Некорректный инвентарь")
            for desc in data.get("descriptions", []):
                descriptions[(str(desc["classid"]), str(desc.get("instanceid", "0")))] = desc
            for asset in data.get("assets", []):
                assets[str(asset["assetid"])] = asset
            if not data.get("more_items"):
                break
            cursor = str(data.get("last_assetid", ""))
            if not cursor or cursor in seen_pages:
                raise SteamError("Steam не отдал следующую страницу инвентаря")
            seen_pages.add(cursor)
        else:
            raise SteamError(
                "Слишком большой инвентарь: проверка прервана без изменения старых данных"
            )
        if "total_inventory_count" in data and len(assets) != int(data["total_inventory_count"]):
            raise SteamError("Инвентарь получен не полностью; прежние данные сохранены")
        result = []
        for asset_id, asset in assets.items():
            desc = descriptions.get((str(asset["classid"]), str(asset.get("instanceid", "0"))))
            tradable = (
                None if desc is None or "tradable" not in desc else bool(int(desc["tradable"]))
            )
            result.append(
                {
                    "assetid": asset_id,
                    "name": plain(
                        (desc or {}).get("market_hash_name") or (desc or {}).get("name") or asset_id
                    ),
                    "amount": max(1, int(asset.get("amount", 1))),
                    "tradable": tradable,
                    "unlock_at": unlock_time(desc or {}),
                    "appid": str(appid),
                    "contextid": str(contextid),
                }
            )
        return result

    def bans(self):
        players = self.api("ISteamUser/GetPlayerBans/v1/", steamids=self.steam_id).get(
            "players", []
        )
        if not players:
            raise SteamError("Steam не вернул ограничения аккаунта")
        player = players[0]
        return {
            "economy_ban": player.get("EconomyBan", "unknown"),
            "community_banned": player.get("CommunityBanned"),
            "vac_bans": player.get("NumberOfVACBans"),
            "game_bans": player.get("NumberOfGameBans"),
        }

    def offers(self):
        response = self.api(
            "IEconService/GetTradeOffers/v1/",
            get_received_offers=1,
            get_sent_offers=1,
            active_only=1,
        ).get("response")
        if not isinstance(response, dict):
            raise SteamError("Steam не вернул список предложений обмена")
        return [
            {
                "id": str(o["tradeofferid"]),
                "direction": direction,
                "state": o.get("trade_offer_state"),
                "partner": o.get("accountid_other"),
                "expires": o.get("expiration_time"),
            }
            for direction, key in (("in", "trade_offers_received"), ("out", "trade_offers_sent"))
            for o in response.get(key, [])
        ]


def inventory_summary(items):
    return {
        "total": sum(v.get("amount", 1) for v in items),
        "tradable": sum(v.get("amount", 1) for v in items if v.get("tradable") is True),
        "locked": sum(v.get("amount", 1) for v in items if v.get("tradable") is False),
        "unknown": sum(v.get("amount", 1) for v in items if v.get("tradable") is None),
    }


class SteamMonitor:
    def __init__(self, path, notify=lambda text: None, client_factory=SteamClient):
        self.path = Path(path)
        self.notify, self.client_factory = notify, client_factory
        self.cache = read_cache(self.path)

    def refresh(self, cfg, remote=True):
        steam_id = str(cfg.get("api_keys", {}).get("steam_id", ""))
        cached = copy.deepcopy(self.cache)
        if cached.get("account") != steam_id:
            cached = {"account": steam_id, "inventories": {}, "notices": []}
        cached.setdefault("inventories", {})
        cached.setdefault("errors", {})
        installed = local_games(cfg.get("steam", {}).get("install_path", ""))
        notices = []
        if remote and steam_id:
            try:
                client = self.client_factory(
                    cfg.get("api_keys", {}).get("steam_api_key", ""), steam_id
                )
            except SteamError as exc:
                cached["errors"]["account"] = str(exc)
            else:
                cached["errors"].pop("account", None)
                for key, fetch in (
                    ("owned", client.owned_games),
                    ("bans", client.bans),
                    ("offers", client.offers),
                ):
                    try:
                        value = fetch()
                        if key == "owned" and "owned" in cached:
                            old_ids = {g["appid"] for g in cached["owned"]}
                            new_games = [g for g in value if g["appid"] not in old_ids]
                            if new_games:
                                notices.append(
                                    f"Steam: новых игр в библиотеке — {len(new_games)}. AppID добавлены в Крест."
                                )
                        if (
                            key == "bans"
                            and cached.get("bans", {}).get("economy_ban") in ("banned", "probation")
                            and value.get("economy_ban") == "none"
                        ):
                            notices.append(
                                "Steam подтвердил снятие ограничения экономики аккаунта. Ограничения отдельных предметов проверяются отдельно."
                            )
                        if key == "offers" and "offers" in cached:
                            old = {v["id"] for v in cached["offers"]}
                            new = [
                                v for v in value if v["direction"] == "in" and v["id"] not in old
                            ]
                            if new:
                                notices.append(
                                    f"Steam: новых входящих предложений обмена — {len(new)}"
                                )
                        cached[key] = value
                        cached[key + "_updated"] = time.time()
                        cached["errors"].pop(key, None)
                    except (SteamError, ValueError, KeyError, TypeError) as exc:
                        cached["errors"][key] = str(exc)
                requested = [
                    v.strip() for v in cfg.get("steam", {}).get("inventories", "730:2").split(",")
                ]
                cached["inventories"] = {
                    k: v for k, v in cached["inventories"].items() if k in requested
                }
                for inventory in requested:
                    try:
                        appid, contextid = inventory.split(":")
                        items = client.inventory(appid, contextid)
                        old = {
                            i["assetid"]: i
                            for i in cached["inventories"].get(inventory, {}).get("items", [])
                        }
                        if inventory in cached["inventories"]:
                            new_ids = {i["assetid"] for i in items}
                            gained = [i for i in items if i["assetid"] not in old]
                            removed = [i for ident, i in old.items() if ident not in new_ids]
                            if gained or removed:
                                notices.append(
                                    f"Steam {appid}: инвентарь изменился · новых предметов {sum(v['amount'] for v in gained)}, исчезло {sum(v['amount'] for v in removed)}. Изменение ID после обмена также учитывается."
                                )
                        released = [
                            i
                            for i in items
                            if i["tradable"] is True
                            and old.get(i["assetid"], {}).get("tradable") is False
                        ]
                        if released:
                            notices.append(
                                f"Steam {appid}: обмен стал доступен для {sum(v['amount'] for v in released)} предметов: "
                                + ", ".join(v["name"] for v in released[:5])
                            )
                        cached["inventories"][inventory] = {
                            "items": items,
                            "summary": inventory_summary(items),
                            "updated": time.time(),
                        }
                        cached["errors"].pop(inventory, None)
                    except (SteamError, ValueError, KeyError, TypeError) as exc:
                        cached["errors"][inventory] = str(exc)
        cached["games"] = merge_games(cached.get("owned", []), installed)
        cached["local_updated"] = time.time()
        cached["notices"] = (
            cached.get("notices", []) + [{"time": time.time(), "text": n} for n in notices]
        )[-100:]
        atomic_write_json(self.path, cached)
        self.cache = cached
        for text in notices:
            self.notify(text)
        return cached


def cached_catalog(state):
    root = state.filepath.parent if state.filepath else Path(__file__).resolve().parent
    cfg = state.config_getter()
    cache = read_cache(root / "steam_state.json")
    owned = (
        cache.get("owned", [])
        if cache.get("account", "") == str(cfg.get("api_keys", {}).get("steam_id", ""))
        else []
    )
    return merge_games(owned, local_games(cfg.get("steam", {}).get("install_path", "")))
