"""Low frequency local OCR of the verified foreground game, without stored images."""

import asyncio
import io
import os
import re
import threading
import time

from game_capture import foreground_game_rect

SCREENS = (
    (
        "mission_complete",
        "Миссия завершена",
        ("миссия выполнена", "миссия завершена", "mission complete", "mission success"),
    ),
    ("mission_failed", "Миссия провалена", ("миссия провалена", "mission failed")),
    (
        "relic_reward",
        "Выбор награды реликвии",
        ("выберите награду", "select a reward", "choose your reward"),
    ),
    ("foundry", "Кузница", ("кузница", "foundry")),
    ("arsenal", "Арсенал", ("арсенал", "arsenal")),
    ("navigation", "Навигация", ("звездная карта", "star chart")),
    (
        "disruption",
        "Сбой · проводники",
        (
            "защитите проводник",
            "защитите проводники",
            "найдите ключи",
            "conduits defended",
            "defend the conduit",
            "conduits activated",
            "защищено проводников",
            "проводников защищено",
            "найдите демолиста",
        ),
    ),
    (
        "survival",
        "Выживание",
        (
            "поддержка жизнеобеспечения",
            "уровень жизнеобеспечения",
            "life support system",
            "life support remaining",
        ),
    ),
    (
        "revive",
        "Ожидание возрождения",
        ("ожидание возрождения", "удерживайте для возрождения", "hold to revive"),
    ),
    (
        "extraction",
        "Эвакуация",
        (
            "доберитесь до эвакуации",
            "направляйтесь к эвакуации",
            "get to extraction",
            "reach extraction",
        ),
    ),
    (
        "exterminate",
        "Зачистка",
        ("врагов осталось", "уничтожьте всех врагов", "enemies remaining", "eliminate all enemies"),
    ),
    ("capture", "Захват", ("захватите цель", "capture the target")),
)


def classify_screen(text):
    normalized = re.sub(r"[^а-яa-z0-9\s]", " ", text.casefold().replace("ё", "е"))
    normalized = re.sub(r"\s+", " ", normalized)
    for code, label, phrases in SCREENS:
        if any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", normalized) for p in phrases):
            return {"screen": code, "label": label}
    return {"screen": "unknown", "label": "Экран не распознан"}


SCREEN_HINTS = {
    "extraction": "Задание указывает на эвакуацию. Соберите нужную добычу и двигайтесь к маркеру выхода.",
    "exterminate": "На задании зачистки следите за оставшимися врагами и маркером цели.",
    "capture": "Захват: доберитесь до отмеченной цели и выполните взаимодействие после её остановки.",
    "disruption": "Сбой: подберите подходящий ключ, активируйте проводник и найдите демолиста по звуку. «Варфрейм аполлон» — план фарма Акси; «открой сводку по миру» — события.",
    "survival": "Следите за жизнеобеспечением и точкой эвакуации. «Мои цели варфрейм» — ваш список фарма.",
    "revive": "Игра показывает возрождение. Проверьте оставшиеся попытки и возможность помощи союзника.",
    "mission_complete": "Миссия завершена. Скажите «варфрейм сохрани момент» или «мои цели варфрейм».",
    "mission_failed": "Проверьте выживаемость и моды. «Варфрейм проверь билд» и название сохранённой сборки.",
    "relic_reward": "Сравните нужные вам компоненты. «Варфрейм рынок» и название предмета — поиск цены.",
    "foundry": "Запомните таймер голосом: «варфрейм кузница форма на двадцать три часа».",
    "arsenal": "Выберите сборку под миссию. «Варфрейм проверь билд» и имя вашей сохранённой сборки.",
    "navigation": "«Варфрейм план на тридцать минут» — план по целям; «варфрейм где фармить нейроды» — справка.",
}


def screen_hint(screen):
    return SCREEN_HINTS.get(
        screen,
        "«Открой сводку по миру» — события; «варфрейм план на тридцать минут» — план; «варфрейм где фармить нейроды» — ресурсы.",
    )


async def _read_image(image):
    from winsdk.windows.graphics.imaging import BitmapDecoder, BitmapPixelFormat, BitmapAlphaMode
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

    languages = list(getattr(OcrEngine, "available_recognizer_languages", ()))
    russian = next((lang for lang in languages if lang.language_tag.lower().startswith("ru")), None)
    engine = (
        OcrEngine.try_create_from_language(russian)
        if russian
        else OcrEngine.try_create_from_user_profile_languages()
    )
    if engine is None:
        preferred = [
            lang for lang in languages if lang.language_tag.lower().startswith(("ru", "en"))
        ]
        for language in preferred or languages:
            engine = OcrEngine.try_create_from_language(language)
            if engine is not None:
                break
    if engine is None:
        raise RuntimeError(
            "В Windows не установлен язык распознавания текста. Добавьте русский или английский язык в параметрах Windows."
        )
    limit = min(1600, int(getattr(OcrEngine, "max_image_dimension", 1600)))
    image.thumbnail((limit, limit))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    bitmap = None
    try:
        writer.write_bytes(buffer.getvalue())
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async(
            BitmapPixelFormat.BGRA8, BitmapAlphaMode.PREMULTIPLIED
        )
        result = await engine.recognize_async(bitmap)
        return result.text
    finally:
        if bitmap is not None:
            bitmap.close()
        writer.close()
        stream.close()


def observe_screen():
    if os.name != "nt":
        raise RuntimeError("Зрение игры доступно в Windows с requirements-windows.txt")
    from PIL import ImageGrab

    bounds = foreground_game_rect()
    image = ImageGrab.grab(bbox=bounds, all_screens=True)
    try:
        if foreground_game_rect() != bounds:
            raise ValueError("Окно игры изменилось; кадр пропущен")
        from game_visual_memory import signature

        feature = signature(image)

        async def read():
            from PIL import ImageOps, ImageDraw
            from warframe_farm import PLAYING, read_facts

            # Read the small objective area at native resolution first. Bright
            # HUD text remains legible even when the full 4K frame is downscaled.
            crop = image.crop((0, 0, int(image.width * 0.58), int(image.height * 0.52)))
            try:
                contrast = ImageOps.autocontrast(crop.convert("L"))
                try:
                    hud = await _read_image(contrast)
                finally:
                    contrast.close()
            finally:
                crop.close()
            result = classify_screen(hud)
            text, region = hud, "панель задания"
            if result["screen"] not in PLAYING:
                full = image.copy()
                try:
                    # Exclude the chat area before OCR; chat must not pretend
                    # to be a mission result or a HUD warning.
                    ImageDraw.Draw(full).rectangle(
                        (0, int(image.height * 0.6), int(image.width * 0.38), image.height),
                        fill="black",
                    )
                    text = await _read_image(full)
                finally:
                    full.close()
                full_result = classify_screen(text)
                if full_result["screen"] != "unknown":
                    result, region = full_result, "весь экран без чата"
                text = hud + " " + text
            return {
                **result,
                "text_chars": len(text.strip()),
                "region": region,
                "facts": read_facts(text, result["screen"]),
            }

        result = asyncio.run(asyncio.wait_for(read(), timeout=10))
        if foreground_game_rect() != bounds:
            raise ValueError("Окно игры изменилось; результат пропущен")
        # Raw text/chat never becomes persistent memory; only a small known screen label does.
        return {**result, "feature": feature}
    finally:
        image.close()


def capture_signature():
    """Explicit screen teaching works independently of installed OCR languages."""
    from PIL import ImageGrab
    from game_visual_memory import signature

    bounds = foreground_game_rect()
    image = ImageGrab.grab(bbox=bounds, all_screens=True)
    try:
        if foreground_game_rect() != bounds:
            raise ValueError("Окно игры изменилось; пример не сохранён")
        return signature(image)
    finally:
        image.close()


class GameVision:
    def __init__(
        self,
        store,
        observe=observe_screen,
        allowed=lambda: True,
        memory=None,
        learning_allowed=lambda: True,
    ):
        self.store, self.observe, self.allowed = store, observe, allowed
        self.memory, self.learning_allowed = memory, learning_allowed
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.thread = None
        self.next_scan = 0
        self.candidate = ""
        self.confirmed = ""
        self.state = {
            "status": "Ожидаю активное окно Warframe",
            "screen": "unknown",
            "label": "",
            "observed": 0,
            "busy": False,
            "attempts": 0,
            "readable": 0,
            "recognized": 0,
            "last_error": "",
        }

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def tick(self, enabled, running, now=None):
        now = time.time() if now is None else now
        if not enabled or not running:
            if self.memory:
                self.memory.forget_pending()
            with self.lock:
                self.state.update(
                    status="Зрение выключено" if not enabled else "Ожидаю запуск Warframe",
                    screen="unknown",
                    label="",
                    visual_guess="unknown",
                    visual_label="",
                    facts={},
                    observed=0,
                )
            self.candidate = self.confirmed = ""
            return
        if self.closed.is_set() or now < self.next_scan or (self.thread and self.thread.is_alive()):
            return
        # Confirm transitions promptly; stable screens need fewer OCR calls.
        self.next_scan = now + (
            3
            if self.candidate != self.confirmed
            or self.candidate in ("relic_reward", "disruption", "survival", "extraction")
            else 8
        )
        self.thread = threading.Thread(target=self.scan, daemon=True, name="krest-game-ocr")
        self.thread.start()

    def scan(self):
        started = time.monotonic()
        with self.lock:
            self.state["busy"] = True
            self.state["attempts"] += 1
        try:
            result = self.observe()
            if self.closed.is_set() or not self.allowed():
                return
            code = result["screen"]
            if code != self.candidate and code != "unknown":
                self.next_scan = min(self.next_scan, time.time() + 3)
            feature = result.pop("feature", None)
            result.update(visual_guess="unknown", visual_label="")
            if self.memory:
                if self.learning_allowed():
                    prediction = self.memory.observe(code, feature)
                    result["visual_guess"] = prediction
                    result["visual_label"] = self.memory.screens.get(prediction, "")
                else:
                    self.memory.forget_pending()
                    prediction = self.memory.predict(feature)
                    result["visual_guess"] = prediction
                    result["visual_label"] = self.memory.screens.get(prediction, "")
            with self.lock:
                status = "Прочитан экран: " + result["label"]
                self.state["readable"] += int(result.get("text_chars", 0) > 0)
                self.state["recognized"] += int(code != "unknown")
                if code == "unknown":
                    status = (
                        "Текст читается, но знакомый экран не найден. Откройте кузницу, арсенал или результаты миссии."
                        if result.get("text_chars", 0)
                        else "На кадре не найден текст. Проверьте язык OCR Windows и видимость интерфейса игры."
                    )
                if code == "unknown" and result["visual_label"]:
                    status = (
                        "По памяти похоже на «" + result["visual_label"] + "»; OCR не подтвердил"
                    )
                self.state.update(result, status=status, observed=time.time(), last_error="")
            if code == self.candidate and code != self.confirmed:
                self.confirmed = code
                if code != "unknown":
                    self.store.add(
                        "journal",
                        "Экран: " + result["label"],
                        notes="Автоматическое наблюдение OCR, подтверждено двумя кадрами. Возможна ошибка распознавания; прогресс и инвентарь не изменены.",
                    )
            self.candidate = code
        except ValueError:
            if self.memory:
                self.memory.forget_pending()
            with self.lock:
                self.state.update(
                    status="Ожидаю активное окно Warframe",
                    screen="unknown",
                    label="",
                    visual_guess="unknown",
                    visual_label="",
                    facts={},
                    observed=0,
                )
            self.candidate = self.confirmed = ""
        except Exception as exc:
            if self.memory:
                self.memory.forget_pending()
            with self.lock:
                self.state.update(
                    status="Зрение недоступно: " + str(exc)[:250],
                    last_error=str(exc)[:250] or type(exc).__name__,
                    screen="unknown",
                    label="",
                    visual_guess="unknown",
                    visual_label="",
                    facts={},
                    observed=0,
                )
            self.next_scan = time.time() + 90
        finally:
            with self.lock:
                self.state["busy"] = False
                self.state["scan_ms"] = round((time.monotonic() - started) * 1000)
                self.state["confirmed"] = self.confirmed

    def close(self):
        self.closed.set()
