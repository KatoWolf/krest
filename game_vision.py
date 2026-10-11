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
)


def classify_screen(text):
    normalized = re.sub(r"[^а-яa-z0-9\s]", " ", text.casefold().replace("ё", "е"))
    normalized = re.sub(r"\s+", " ", normalized)
    for code, label, phrases in SCREENS:
        if any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", normalized) for p in phrases):
            return {"screen": code, "label": label}
    return {"screen": "unknown", "label": "Экран не распознан"}


SCREEN_HINTS = {
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

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        languages = list(OcrEngine.available_recognizer_languages)
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
    image.thumbnail(
        (min(1600, OcrEngine.max_image_dimension), min(1600, OcrEngine.max_image_dimension))
    )
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
        text = asyncio.run(asyncio.wait_for(_read_image(image), timeout=8))
        if foreground_game_rect() != bounds:
            raise ValueError("Окно игры изменилось; результат пропущен")
        # Raw text/chat never becomes persistent memory; only a small known screen label does.
        return {**classify_screen(text), "feature": feature}
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
                )
            self.candidate = self.confirmed = ""
            return
        if self.closed.is_set() or now < self.next_scan or (self.thread and self.thread.is_alive()):
            return
        self.next_scan = now + 15
        self.thread = threading.Thread(target=self.scan, daemon=True, name="krest-game-ocr")
        self.thread.start()

    def scan(self):
        with self.lock:
            self.state["busy"] = True
        try:
            result = self.observe()
            if self.closed.is_set() or not self.allowed():
                return
            code = result["screen"]
            feature = result.pop("feature", None)
            result.update(visual_guess="unknown", visual_label="")
            if self.memory:
                if self.learning_allowed():
                    prediction = self.memory.observe(code, feature)
                    result["visual_guess"] = prediction
                    result["visual_label"] = self.memory.screens.get(prediction, "")
                else:
                    self.memory.forget_pending()
            with self.lock:
                status = "Прочитан экран: " + result["label"]
                if code == "unknown" and result["visual_label"]:
                    status = (
                        "По памяти похоже на «" + result["visual_label"] + "»; OCR не подтвердил"
                    )
                self.state.update(result, status=status, observed=time.time())
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
                )
            self.candidate = self.confirmed = ""
        except Exception as exc:
            if self.memory:
                self.memory.forget_pending()
            with self.lock:
                self.state.update(
                    status="Зрение недоступно: " + str(exc)[:250],
                    screen="unknown",
                    label="",
                    visual_guess="unknown",
                    visual_label="",
                )
            self.next_scan = time.time() + 90
        finally:
            with self.lock:
                self.state["busy"] = False

    def close(self):
        self.closed.set()
