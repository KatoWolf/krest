import os
import sys
import json
import re

try:
    import winreg
except ImportError:
    winreg = None
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
import webbrowser
import logging
from typing import Dict, List, Optional, Tuple, Any


from skill_manager import skill  # type: ignore # pyright: ignore

import voice

logger = logging.getLogger("SkillWebAndSteam")

DEFAULT_WEATHER_CITY = "Москва"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KrestAssistant/5.1"

# ==========================================
# 1. СЛОВАРЬ И АДАПТАЦИЯ АНГЛИЙСКИХ НАЗВАНИЙ ИГР
# ==========================================

# База популярного сленга и русских названий для игр в Steam
POPULAR_GAME_ALIASES: Dict[str, List[str]] = {
    # Игры из твоей библиотеки
    "dota 2": ["дота", "дота 2", "дб", "dota", "дотка"],
    "counter-strike 2": [
        "кс",
        "кс 2",
        "контра",
        "ксго",
        "cs go",
        "cs2",
        "counter strike",
        "кс го",
    ],
    "garry's mod": [
        "гаррис мод",
        "гарис мод",
        "гаррис",
        "гарис",
        "гмод",
        "gmod",
        "garrys mod",
    ],
    "dead by daylight": ["дбд", "дед бай делайт", "дед баи делайт", "dbd"],
    "kenshi": ["кенши", "кенси"],
    "mount & blade ii: bannerlord": [
        "баннерлорд",
        "маунт энд блейд",
        "миб 2",
        "баннер лорд",
        "маунт блейд 2",
        "mount and blade",
    ],
    "norland": ["норланд"],
    "nte: neverness to everness": ["neverness to everness", "nte", "невернесс", "нте"],
    "peak": ["пик", "peak"],
    "r.e.p.o.": ["репо", "repo", "р е п о"],
    "terraria": ["террария", "терария", "тера"],
    "unturned": ["антурнед", "анчернед", "антернед", "кубический раст"],
    "valheim": ["валхейм", "валгейм", "вальхейм", "викинги"],
    "war selection": ["вар селекшн", "варселекшн"],
    "warframe": ["варфрейм", "варфреймз", "варф"],
    "wargame: airland battle": ["варгам", "варгайм", "варгайм эйрленд", "wargame"],
    "how to fish": ["хау ту фиш", "рыбалка", "как рыбачить"],
    "miside": ["мисайд", "майсайд"],
    "obs studio": ["обс", "обс студио", "obs"],
    "oxygen not included": [
        "оксиген нот инклюдед",
        "оксиген",
        "кислород",
        "кислород не включен",
        "oni",
    ],
    "phasmophobia": ["фазмофобия", "фазма", "фазмо", "призраки"],
    "soundpad": ["саундпад", "саундпэд"],
    "wallpaper engine": ["воллпейпер инджин", "обои", "живые обои", "ваппапер"],
    # Остальные популярные игры
    "the witcher 3: wild hunt": ["ведьмак 3", "ведьмак", "дикая охота", "witcher"],
    "cyberpunk 2077": ["киберпанк", "киберпанк 2077", "cyberpunk"],
    "grand theft auto v": ["гта 5", "гта", "gta 5", "гта в", "автошторм"],
    "baldurs gate 3": ["балдурс гейт 3", "балдурка", "балдурс гейт", "врата балдура"],
    "euro truck simulator 2": [
        "евро трек",
        "евро трак",
        "дальнобойщики",
        "траки",
        "етс 2",
        "ets 2",
    ],
    "apex legends": ["апекс", "апекс легендс", "apex"],
    "pubg: battlegrounds": ["пабг", "пубг", "пабджи", "pubg"],
    "rust": ["раст", "руст"],
    "minecraft": ["майнкрафт", "майн"],
    "war thunder": ["тундра", "вар тандер", "варвандер", "танки тундра"],
    "world of tanks": ["танки", "ворлд оф танкс", "wot"],
    "sid meiers civilization vi": ["цивилизация 6", "цива 6", "цивилизация"],
    "red dead redemption 2": ["рдр 2", "ред дед", "рдр", "ред дед редемпшн"],
    "hollow knight": ["холлоу найт", "полый рыцарь", "холоу найт"],
    "stardew valley": ["стардев валли", "стардью", "ферма"],
    "cities: skylines": ["ситис скайлайн", "ситис скайлайнс", "градостроитель"],
    "fallout 4": ["фоллаут 4", "фолыч 4", "фолаут"],
    "left 4 dead 2": ["лефт 4 дед 2", "лефтфодед", "л4д2"],
}

# Таблица транслитерации для автоматического сопоставления устной речи
CYR_TO_LAT_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ы": "y",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def transliterate_ru_to_en(text: str) -> str:
    """Преобразует русскую транскрипцию названия в латиницу для нечеткого поиска."""
    res = []
    for char in text.lower():
        res.append(CYR_TO_LAT_MAP.get(char, char))
    return "".join(res)


# ==========================================
# 2. ДЕТЕКЦИЯ И ПАРСИНГ БИБЛИОТЕКИ STEAM
# ==========================================


def get_steam_install_path() -> Optional[str]:
    """Находит главный путь установки Steam через реестр Windows или стандартные папки."""
    try:
        # 1. Поиск в HKCU
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        path, _ = winreg.QueryValueEx(key, "SteamPath")
        winreg.CloseKey(key)
        if path and os.path.exists(path):
            return os.path.normpath(path)
    except Exception:
        pass

    try:
        # 2. Поиск в HKLM (64-bit)
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")
        path, _ = winreg.QueryValueEx(key, "InstallPath")
        winreg.CloseKey(key)
        if path and os.path.exists(path):
            return os.path.normpath(path)
    except Exception:
        pass

    # 3. Резервные пути по умолчанию
    default_paths = [
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        r"D:\Steam",
        r"E:\Steam",
    ]
    for p in default_paths:
        if os.path.exists(p):
            return p

    return None


def get_all_steam_library_folders() -> List[str]:
    """Находит все папки библиотек Steam на всех дисках ПК из файла libraryfolders.vdf."""
    steam_path = get_steam_install_path()
    if not steam_path:
        return []

    libraries = [steam_path]
    vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")

    if os.path.exists(vdf_path):
        try:
            with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # Извлечение всех путей через регулярное выражение VDF
            matches = re.findall(r'"path"\s+"([^"]+)"', content, re.IGNORECASE)
            for m in matches:
                clean_path = os.path.normpath(m.replace("\\\\", "\\"))
                if os.path.exists(clean_path) and clean_path not in libraries:
                    libraries.append(clean_path)
        except Exception as e:
            logger.error(f"Ошибка при чтении libraryfolders.vdf: {e}")

    return libraries


def scan_installed_steam_games() -> List[Dict[str, str]]:
    """
    Сканирует манифесты игр (appmanifest_*.acf) во всех папках библиотек Steam.
    Возвращает список словарей: [{'appid': '570', 'name': 'Dota 2', 'path': '...'}, ...]
    """
    libraries = get_all_steam_library_folders()
    installed_games = []

    for lib in libraries:
        steamapps = os.path.join(lib, "steamapps")
        if not os.path.exists(steamapps):
            continue

        try:
            for item in os.listdir(steamapps):
                if item.startswith("appmanifest_") and item.endswith(".acf"):
                    acf_path = os.path.join(steamapps, item)
                    try:
                        with open(acf_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()

                        appid_match = re.search(r'"appid"\s+"(\d+)"', content, re.IGNORECASE)
                        name_match = re.search(r'"name"\s+"([^"]+)"', content, re.IGNORECASE)

                        if appid_match and name_match:
                            appid = appid_match.group(1)
                            name = name_match.group(1).strip()

                            # Пропускаем служебные утилиты Steam (Proton, Steamworks и т.д.)
                            if any(
                                ignored in name.lower()
                                for ignored in [
                                    "steamworks",
                                    "proton",
                                    "redistributable",
                                    "shared",
                                ]
                            ):
                                continue

                            installed_games.append({"appid": appid, "name": name, "library": lib})
                    except Exception as ex:
                        logger.debug(f"Ошибка чтения манифеста {acf_path}: {ex}")
        except Exception as e:
            logger.error(f"Ошибка сканирования директории {steamapps}: {e}")

    return installed_games


def match_game_by_user_input(
    spoken_name: str, installed_games: List[Dict[str, str]]
) -> Optional[Dict[str, str]]:
    """Resolve complete catalog names or unique, complete aliases."""
    from name_vocabulary import key

    query = key(spoken_name)
    if not query:
        return None
    exact = {str(g["appid"]): g for g in installed_games if key(g["name"]) == query}
    if len(exact) == 1:
        return next(iter(exact.values()))
    aliases = {key(name): {key(v) for v in values} for name, values in POPULAR_GAME_ALIASES.items()}
    aliases.setdefault("counter strike 2", set()).update({"контру", "кс два", "ка эс два"})
    aliases.setdefault("warframe", set()).add("вар фрейм")
    matches = {
        str(g["appid"]): g for g in installed_games if query in aliases.get(key(g["name"]), set())
    }
    if len(matches) == 1:
        return next(iter(matches.values()))
    # A partial name is only useful when it identifies exactly one game. Never
    # launch a different game on a 45% spelling similarity or a substring alias.
    if len(query) >= 4 and not matches:
        for game in installed_games:
            title = key(game["name"])
            if title.startswith(query + " "):
                matches[str(game["appid"])] = game
    return next(iter(matches.values())) if len(matches) == 1 else None


# ==========================================
# 3. ГОЛОСОВЫЕ НАВЫКИ УПРАВЛЕНИЯ ИГРАМИ
# ==========================================


@skill(
    patterns=[
        r"запусти игру (?P<game_name>.+)",
        r"включи игру (?P<game_name>.+)",
        r"сыграем в (?P<game_name>.+)",
        r"поиграем в (?P<game_name>.+)",
        r"запусти (?P<game_name>.+)",
    ],
    keywords=["запусти игру", "включи игру", "сыграем в", "открой игру", "поиграть в"],
    description="Автоматический поиск и запуск любой установленной игры Steam по названию",
    category="Steam & Gaming",
)
def launch_steam_game(text: str, game_name: str = ""):
    """Запускает игру из Steam по устному названию."""
    target_query = game_name.strip() if game_name else text

    # Очистка служебных слов
    for stop_word in [
        "запусти игру",
        "включи игру",
        "сыграем в",
        "поиграем в",
        "открой игру",
        "запусти",
        "включи",
        "крест",
    ]:
        target_query = target_query.lower().replace(stop_word, "").strip()

    if not target_query:
        voice.speak("Какую именно игру вы хотите запустить?")
        return

    voice.speak("Проверяю библиотеку игр Steam...")
    from app_state import get_state
    from steam_service import cached_catalog

    games = cached_catalog(get_state())

    if not games:
        voice.speak("Установленные игры Steam не обнаружены или библиотека недоступна.")
        return False

    matched_game = match_game_by_user_input(target_query, games)

    if matched_game:
        appid = matched_game["appid"]
        title = matched_game["name"]
        voice.speak(f"Запускаю игру {title}.")
        logger.info(f"Запуск Steam игры: {title} (AppID: {appid})")

        # Запуск через родовой протокол Steam
        if not webbrowser.open(f"steam://rungameid/{appid}"):
            return {"status": "error", "message": "Не удалось открыть протокол Steam."}
    else:
        voice.speak(f"Не удалось найти игру в библиотеке по запросу '{target_query}'.")
        return False


@skill(
    keywords=[
        "какие игры установлены",
        "список игр в стиме",
        "мои игры в стим",
        "покажи установленные игры",
        "игры стим",
    ],
    description="Озвучивание и вывод полного списка скачанных игр из библиотеки Steam",
    category="Steam & Gaming",
)
def list_installed_games(text: str = ""):
    """Озвучивает и выводит список всех найденных игр Steam."""
    games = scan_installed_steam_games()

    if not games:
        voice.speak("На вашем ПК не найдено установленных игр Steam.")
        return

    game_names = [g["name"] for g in games]
    count = len(game_names)

    print("\n" + "=" * 50)
    print(f"🎮 НАЙДЕННЫЕ ИГРЫ В БИБЛИОТЕКЕ STEAM ({count}):")
    print("=" * 50)
    for idx, name in enumerate(game_names, 1):
        print(f"  {idx}. {name}")
    print("=" * 50 + "\n")

    top_games = ", ".join(game_names[:4])
    if count > 4:
        voice.speak(
            f"Найдено {count} установленных игр. Среди них: {top_games} и другие. Полный список выведен в консоль."
        )
    else:
        voice.speak(f"Найдено игр: {count}. Это: {top_games}.")


# ==========================================
# 4. ВЕБ-НАВЫКИ (ПОГОДА, ВИКИПЕДИЯ, НОВОСТИ)
# ==========================================


def fetch_weather_info(city_name: str) -> dict:
    """Запрашивает актуальный прогноз погоды через API wttr.in."""
    encoded_city = urllib.parse.quote(city_name)
    url = f"https://wttr.in/{encoded_city}?format=j1&lang=ru"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            from network_reads import read_response

            data = json.loads(read_response(resp).decode("utf-8"))
            current = data["current_condition"][0]

            temp_c = current.get("temp_C", "0")
            feels_like = current.get("FeelsLikeC", "0")
            wind_speed = current.get("windspeedKmh", "0")

            lang_ru = current.get("lang_ru", [])
            condition = (
                lang_ru[0]["value"]
                if lang_ru
                else current.get("weatherDesc", [{}])[0].get("value", "Ясно")
            )

            return {
                "success": True,
                "city": city_name.capitalize(),
                "temp": temp_c,
                "feels_like": feels_like,
                "condition": condition.lower(),
                "wind": wind_speed,
            }
    except Exception as e:
        logger.error(f"Weather API request error: {e}")
        return {"success": False, "error": str(e)}


@skill(
    patterns=[
        r"какая погода в (?P<city>.+)",
        r"погода в (?P<city>.+)",
        r"прогноз погоды в (?P<city>.+)",
        r"прогноз погоды (?P<city>.+)",
    ],
    keywords=[
        "какая погода",
        "погода на улице",
        "температура на улице",
        "прогноз погоды",
    ],
    description="Получение актуального прогноза погоды и температуры для города",
    category="Web & Info",
)
def get_weather_forecast(text: str, city: str = ""):
    from app_state import get_state

    default_city = (
        get_state()
        .config_getter()
        .get("app_settings", {})
        .get("weather_city", DEFAULT_WEATHER_CITY)
    )
    target_city = city.strip() if city else default_city

    if not city:
        words = text.lower().split()
        if "в" in words:
            idx = words.index("в")
            if idx + 1 < len(words):
                target_city = words[idx + 1]

    voice.speak(f"Запрашиваю прогноз погоды для города {target_city}.")
    info = fetch_weather_info(target_city)

    if info["success"]:
        report = (
            f"Сейчас в городе {info['city']} {info['condition']}. "
            f"Температура воздуха {info['temp']} градусов, "
            f"ощущается как {info['feels_like']}. "
            f"Скорость ветра {info['wind']} километров в час."
        )
        voice.speak(report)
    else:
        voice.speak(f"Не удалось получить данные о погоде для города {target_city}.")
        return False


def fetch_wikipedia_summary(query_term: str) -> str:
    """Запрашивает выжимку статьи из русской Википедии."""
    encoded_query = urllib.parse.quote(query_term)
    url = (
        f"https://ru.wikipedia.org/w/api.php?action=query&prop=extracts"
        f"&exintro=1&explaintext=1&redirects=1&format=json&titles={encoded_query}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            from network_reads import read_response

            data = json.loads(read_response(resp).decode("utf-8"))
            pages = data.get("query", {}).get("pages", {})
            for _, page in pages.items():
                if "extract" in page and page["extract"].strip():
                    extract = page["extract"].strip()
                    sentences = re.split(r"(?<=[.!?])\s+", extract)
                    summary = " ".join(sentences[:3])
                    return summary
    except Exception as e:
        logger.error(f"Wikipedia API error: {e}")

    return ""


@skill(
    patterns=[
        r"что такое (?P<query>.+)",
        r"кто такой (?P<query>.+)",
        r"кто такая (?P<query>.+)",
        r"найди в википедии (?P<query>.+)",
        r"википедия (?P<query>.+)",
    ],
    description="Быстрый поиск определения или биографии в Википедии",
    examples=["что такое фотосинтез"],
    category="Web & Info",
)
def search_wikipedia(text: str, query: str = ""):
    if not query:
        voice.speak("Укажите предмет или имя для поиска в Википедии.")
        return

    clean_query = query.strip()
    voice.speak(f"Ищу в Википедии информацию о {clean_query}.")

    summary = fetch_wikipedia_summary(clean_query)

    if summary:
        voice.speak(summary[:350])
    else:
        voice.speak(
            f"Не удалось получить статью о {clean_query} из Википедии. Открываю поиск в браузере."
        )
        webbrowser.open(f"https://yandex.ru/search/?text={urllib.parse.quote(clean_query)}")


@skill(
    patterns=[
        r"найди в интернете (?P<query>.+)",
        r"найди в яндексе (?P<query>.+)",
        r"поищи в гугле (?P<query>.+)",
        r"поищи (?P<query>.+)",
        r"гугл (?P<query>.+)",
    ],
    keywords=["поиск в сети", "найди в инете", "открой поиск"],
    description="Поиск информации в Яндекс / Google с открытием вкладки в браузере",
    category="Web & Info",
)
def search_web_query(text: str, query: str = ""):
    if not query:
        search_terms = re.sub(
            r"^(найди|поищи|гугл|яндекс|в интернете|в сети)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
    else:
        search_terms = query.strip()

    if not search_terms:
        voice.speak("Введите или скажите поисковый запрос.")
        return

    voice.speak(f"Ищу в интернете: {search_terms}.")
    encoded = urllib.parse.quote(search_terms)

    if "гугл" in text.lower() or "google" in text.lower():
        url = f"https://www.google.com/search?q={encoded}"
    else:
        url = f"https://yandex.ru/search/?text={encoded}"

    webbrowser.open(url)


def fetch_top_news_headlines(limit: int = 3) -> list:
    """Получает заголовки новостей из RSS ленты Lenta.ru."""
    rss_url = "https://lenta.ru/rss/news"
    req = urllib.request.Request(rss_url, headers={"User-Agent": USER_AGENT})

    titles = []
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            from network_reads import read_response

            xml_data = read_response(resp)
            root = ET.fromstring(xml_data)
            for item in root.findall("./channel/item"):
                title_elem = item.find("title")
                if title_elem is not None and title_elem.text:
                    clean_title = title_elem.text.strip()
                    titles.append(clean_title)
                if len(titles) >= limit:
                    break
    except Exception as e:
        logger.error(f"Error reading RSS news: {e}")

    return titles


@skill(
    keywords=[
        "последние новости",
        "свежие новости",
        "главные новости",
        "что нового в мире",
        "новости",
    ],
    description="Озвучивание главных свежих новостей дня из RSS ленты",
    category="Web & Info",
)
def read_latest_news(text: str):
    voice.speak("Получаю свежие новости.")

    headlines = fetch_top_news_headlines(limit=3)

    if headlines:
        voice.speak("Вот главные новости к этому часу:")
        for idx, item in enumerate(headlines, start=1):
            voice.speak(f"Новость номер {idx}: {item}.")
    else:
        voice.speak("Не удалось загрузить новости. Открываю новостной портал в браузере.")
        webbrowser.open("https://dzen.ru/news")


@skill(
    patterns=[r"открой сайт (?P<site_name>.+)", r"перейди на (?P<site_name>.+)"],
    description="Быстрый переход на популярные сайты и порталы",
    examples=["открой сайт ютуб"],
    category="Web & Info",
)
def navigate_to_popular_site(text: str, site_name: str = ""):
    if not site_name:
        voice.speak("Укажите адрес или название сайта.")
        return

    clean_site = site_name.lower().strip()

    known_sites = {
        "хабр": "https://habr.com",
        "пикабу": "https://pikabu.ru",
        "вконтакте": "https://vk.com",
        "вк": "https://vk.com",
        "авито": "https://avito.ru",
        "почта": "https://mail.ru",
        "новости": "https://dzen.ru/news",
        "стим": "https://store.steampowered.com",
        "ютуб": "https://youtube.com",
        "гитхаб": "https://github.com",
    }

    target_url = known_sites.get(clean_site)

    if not target_url:
        if "." in clean_site and " " not in clean_site:
            target_url = clean_site if clean_site.startswith("http") else f"https://{clean_site}"
        else:
            target_url = f"https://yandex.ru/search/?text={urllib.parse.quote(clean_site)}"

    voice.speak(f"Открываю {clean_site}.")
    webbrowser.open(target_url)
