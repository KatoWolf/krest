"""Small local visual prototypes taught by OCR, never by their own predictions."""

import math
import hashlib
from pathlib import Path
import threading

from storage import atomic_write_json, read_json

DIMENSIONS = 16 * 9 * 3
MAX_PER_SCREEN = 8


def signature(image):
    from PIL import Image

    rgb = image.convert("RGB")
    try:
        small = rgb.resize((16, 9), Image.Resampling.BOX)
        try:
            values = list(small.tobytes())
        finally:
            small.close()
    finally:
        rgb.close()
    return {"values": values, "aspect": round(image.width / image.height, 2)}


def valid_signature(value):
    if not isinstance(value, dict):
        return False
    values = value.get("values")
    aspect = value.get("aspect")
    return (
        isinstance(values, list)
        and len(values) == DIMENSIONS
        and all(type(v) is int and 0 <= v <= 255 for v in values)
        and type(aspect) in (int, float)
        and math.isfinite(aspect)
        and 0.5 <= aspect <= 5
    )


def distance(left, right):
    if abs(left["aspect"] - right["aspect"]) > 0.03:
        return 1.0
    return sum(abs(a - b) for a, b in zip(left["values"], right["values"])) / (DIMENSIONS * 255)


class VisualMemory:
    def __init__(self, path, screens):
        self.path = Path(path)
        self.screens = dict(screens)
        self.lock = threading.RLock()
        self.pending = None
        self.data = {"version": 1, "examples": {}, "checks": 0, "matches": 0}
        self.error = ""
        if self.path.exists():
            try:
                if self.path.stat().st_size > 300000:
                    raise ValueError("Память экранов слишком большая")
                value = read_json(self.path)
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise ValueError("Неизвестный формат памяти экранов")
                examples = value.get("examples")
                if not isinstance(examples, dict) or any(
                    key not in self.screens for key in examples
                ):
                    raise ValueError("Повреждён набор экранов")
                for rows in examples.values():
                    if (
                        not isinstance(rows, list)
                        or len(rows) > MAX_PER_SCREEN
                        or any(not valid_signature(row) for row in rows)
                    ):
                        raise ValueError("Повреждён визуальный пример")
                for key in ("checks", "matches"):
                    if type(value.get(key)) is not int or not 0 <= value[key] <= 10**9:
                        raise ValueError("Повреждён счётчик проверок")
                if value["matches"] > value["checks"]:
                    raise ValueError("Повреждён счётчик совпадений")
                for key in ("user_checks", "user_matches", "manual_examples"):
                    if type(value.get(key, 0)) is not int or not 0 <= value.get(key, 0) <= 10**9:
                        raise ValueError("Повреждён счётчик личных проверок")
                if value.get("user_matches", 0) > value.get("user_checks", 0):
                    raise ValueError("Повреждён счётчик личных совпадений")
                for key in ("checked_images", "ocr_checked_images"):
                    if (
                        not isinstance(value.get(key, []), list)
                        or len(value.get(key, [])) > 100
                        or any(not isinstance(v, str) or len(v) != 64 for v in value.get(key, []))
                    ):
                        raise ValueError("Повреждена история проверок")
                self.data = value
            except (OSError, ValueError, TypeError) as exc:
                self.error = str(exc)

    def predict(self, feature):
        if not valid_signature(feature):
            return "unknown"
        with self.lock:
            scores = sorted(
                (min(distance(feature, row) for row in rows), code)
                for code, rows in self.data["examples"].items()
                if rows
            )
            if not scores or scores[0][0] > 0.035:
                return "unknown"
            if len(scores) > 1 and scores[1][0] - scores[0][0] < 0.02:
                return "unknown"
            return scores[0][1]

    def observe(self, code, feature):
        """Evaluate against a new OCR result before adding that sample to memory."""
        if not valid_signature(feature):
            self.forget_pending()
            return "unknown"
        with self.lock:
            predicted = self.predict(feature)
            if self.error:
                return predicted
            if code not in self.screens:
                self.pending = None
                return predicted
            digest = hashlib.sha256(bytes(feature["values"])).hexdigest()
            checked = self.data.setdefault("ocr_checked_images", [])
            is_new = not any(
                distance(feature, row) <= 0.008
                for rows in self.data["examples"].values()
                for row in rows
            )
            if self.data["examples"] and is_new and digest not in checked:
                self.data["checks"] += 1
                self.data["matches"] += int(predicted == code)
                checked.append(digest)
                del checked[:-100]
            previous = self.pending
            self.pending = (code, feature)
            # Reject blank/loading frames and require two visually consistent OCR labels.
            values = feature["values"]
            mean = sum(values) / len(values)
            contrast = sum(abs(v - mean) for v in values) / len(values)
            if (
                previous
                and previous[0] == code
                and contrast >= 8
                and distance(previous[1], feature) <= 0.05
            ):
                rows = self.data["examples"].setdefault(code, [])
                if not rows or min(distance(feature, row) for row in rows) > 0.008:
                    rows.append(feature)
                    del rows[:-MAX_PER_SCREEN]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(self.path, self.data)
            except OSError as exc:
                self.error = str(exc)
            return predicted

    def forget_pending(self):
        with self.lock:
            self.pending = None

    def teach(self, code, feature):
        """One explicit label bootstraps recognition even when OCR cannot read the HUD."""
        if code not in self.screens or not valid_signature(feature):
            raise ValueError("Выберите известный тип экрана и активное окно Warframe")
        values = feature["values"]
        mean = sum(values) / len(values)
        if sum(abs(v - mean) for v in values) / len(values) < 8:
            raise ValueError("Кадр почти пустой. Дождитесь появления интерфейса игры")
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            import copy

            data = copy.deepcopy(self.data)
            rows = data["examples"].setdefault(code, [])
            if not rows or min(distance(feature, row) for row in rows) > 0.008:
                rows.append(feature)
                del rows[:-MAX_PER_SCREEN]
            data["manual_examples"] = int(data.get("manual_examples", 0)) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, data)
            self.data = data
        return (
            "Сохранён пример «"
            + self.screens[code]
            + "». Проверьте узнавание при следующем открытии этого экрана."
        )

    def evaluate(self, code, feature):
        """Explicit user check is separate from teaching; repeated images don't inflate it."""
        if code not in self.screens or not valid_signature(feature):
            raise ValueError("Некорректный экран для проверки")
        import hashlib
        import copy

        digest = hashlib.sha256(bytes(feature["values"])).hexdigest()
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            predicted = self.predict(feature)
            # Near-identical training samples do not establish generalization.
            if any(
                distance(feature, row) <= 0.008
                for rows in self.data["examples"].values()
                for row in rows
            ):
                return "Кадр почти совпадает с обучающим. Откройте этот экран в другой игровой ситуации; проверка не засчитана."
            data = copy.deepcopy(self.data)
            checked = data.setdefault("checked_images", [])
            if digest in checked:
                return "Этот кадр уже проверялся. Счётчик не изменён."
            checked.append(digest)
            del checked[:-100]
            data["user_checks"] = int(data.get("user_checks", 0)) + 1
            data["user_matches"] = int(data.get("user_matches", 0)) + int(predicted == code)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, data)
            self.data = data
            return "Проверка на новом кадре: " + (
                "узнала правильно — " + self.screens[code]
                if predicted == code
                else "не совпало. Добавьте этот пример через «запомни экран». Самостоятельно его не запоминаю."
            )

    def snapshot(self):
        with self.lock:
            return {
                "screens": [
                    self.screens[key] for key, rows in self.data["examples"].items() if rows
                ],
                "examples": sum(map(len, self.data["examples"].values())),
                "checks": self.data["checks"],
                "matches": self.data["matches"],
                "error": self.error,
                "user_checks": self.data.get("user_checks", 0),
                "user_matches": self.data.get("user_matches", 0),
            }

    def report(self):
        info = self.snapshot()
        return "\n".join(
            [
                "Визуальная память: "
                + (", ".join(info["screens"]) or "ещё нет повторных примеров"),
                f"Эталонов: {info['examples']}; совпадений с новым OCR: {info['matches']} из {info['checks']}.",
                f"Проверок вами на отличающихся кадрах: {info['user_checks']}; верных: {info['user_matches']}.",
                "Это сверка с OCR, а не доказательство понимания боя. Похожий кадр даёт только предположение.",
                info["error"],
            ]
        ).strip()
