"""Small OCR mission journal. Observations never masquerade as owned loot."""

import copy
import re
import threading
import time

from storage import atomic_write_json, read_json

PLAYING = {"disruption", "survival", "exterminate", "capture", "extraction"}
RESULTS = {"mission_complete", "mission_failed"}


def read_facts(text, screen):
    """Extract only bounded, labelled HUD facts; discard all other OCR text."""
    value = re.sub(r"\s+", " ", text.casefold().replace("ё", "е"))
    facts = {}
    patterns = {
        "round": (r"(?:раунд|round)\s*[:№#-]?\s*(\d{1,2})(?!\d)", 1, 99),
        "conduits": (
            r"(?:защищено проводников|проводников защищено|conduits defended)\s*[:\-]?\s*([0-4])(?:\s*/\s*4)?(?!\d)",
            0,
            4,
        ),
        "life_support": (
            r"(?:уровень жизнеобеспечения|жизнеобеспечение|life support(?: remaining| system)?)\s*[:\-]?\s*(\d{1,3})\s*%",
            0,
            100,
        ),
    }
    for name, (pattern, low, high) in patterns.items():
        match = re.search(pattern, value)
        if match and low <= int(match[1]) <= high:
            facts[name] = int(match[1])
    if screen in RESULTS:
        relics = set()
        for match in re.finditer(
            r"\b(акси|axi|нео|neo|мезо|meso|лит|lith)\s+([a-zа-я]\d{1,2})\b", value
        ):
            era = {
                "акси": "Акси",
                "axi": "Акси",
                "нео": "Нео",
                "neo": "Нео",
                "мезо": "Мезо",
                "meso": "Мезо",
                "лит": "Лит",
                "lith": "Лит",
            }[match[1]]
            code = match[2].upper().translate(str.maketrans("АВСЕНКМОРТХ", "ABCEHKMOPTX"))
            relics.add(era + " " + code)
        facts["relics"] = sorted(relics)[:12]
    return facts


class FarmJournal:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.rows = []
        self.error = ""
        try:
            data = read_json(path) if path.exists() else {}
            rows = data.get("runs", [])
            if (
                not isinstance(rows, list)
                or len(rows) > 100
                or any(
                    not isinstance(row, dict)
                    or row.get("result") not in RESULTS
                    or any(
                        type(row.get(k)) not in (int, float) or row[k] < 0
                        for k in ("started", "ended", "round")
                    )
                    or row["ended"] < row["started"]
                    or not isinstance(row.get("relics_seen"), list)
                    or len(row["relics_seen"]) > 12
                    or any(not isinstance(v, str) or len(v) > 30 for v in row["relics_seen"])
                    for row in rows
                )
            ):
                raise ValueError("Повреждён журнал забегов")
            self.rows = rows[-100:]
        except (ValueError, OSError) as exc:
            self.error = str(exc)
        self.active = None
        self.hud = {}
        self.last_at = 0
        self.previous = None
        self.last_result = None
        self._warning_at = 0

    def observe(self, observation, profile=None):
        at = observation.get("observed", 0)
        if not at or at <= self.last_at or time.time() - at > 30:
            return None
        with self.lock:
            self.last_at = at
            code = observation.get("screen", "unknown")
            facts = observation.get("facts", {})
            previous = self.previous
            self.previous = (code, facts)
            if not previous or previous[0] != code or code == "unknown":
                self.hud = {}
                return None
            # Every number/reward name requires two independent OCR reads.
            stable = {k: v for k, v in facts.items() if previous[1].get(k) == v}
            self.hud = stable
            if code in PLAYING and self.active is None:
                self.active = {
                    "started": at,
                    "mode": code,
                    "profile": str(profile or "")[:160],
                    "round": 0,
                }
                self.last_result = None
            if self.active:
                self.active["round"] = max(self.active["round"], stable.get("round", 0))
            if code in RESULTS and self.active:
                row = {
                    **self.active,
                    "ended": at,
                    "result": code,
                    "relics_seen": stable.get("relics", []),
                }
                self.rows = [*self.rows[-99:], row]
                self.active, self.last_result = None, row
                self._save()
                if self.error:
                    return "Замечен результат миссии, но журнал не удалось записать на диск. Подробности в меню Warframe."
                return (
                    "Миссия завершена. Отчёт сохранён. «Крест, отчёт фарма» — подробности."
                    if code == "mission_complete"
                    else "Миссия провалена. Забег записан в журнал фарма."
                )
            if code in RESULTS and self.last_result and stable.get("relics"):
                if self.last_result["relics_seen"] != stable["relics"]:
                    self.last_result["relics_seen"] = stable["relics"]
                    self._save()
            if stable.get("life_support", 100) <= 30 and at - self._warning_at >= 60:
                self._warning_at = at
                return f"Жизнеобеспечение: {stable['life_support']} процентов. Проверьте капсулу или выход."
            return None

    def _save(self):
        try:
            atomic_write_json(self.path, {"version": 1, "runs": self.rows})
            self.error = ""
        except OSError as exc:
            self.error = str(exc)

    def suspend(self, stopped=False):
        with self.lock:
            self.previous = None
            self.hud = {}
            if stopped:
                self.active = None

    def summary(self):
        with self.lock:
            lines = []
            if self.active:
                minutes = max(0, int((time.time() - self.active["started"]) / 60))
                lines.append(f"Текущий забег: {minutes} мин с первого распознанного задания.")
            if "round" in self.hud:
                lines.append(f"Раунд: {self.hud['round']}.")
            if "conduits" in self.hud:
                lines.append(f"Защищено проводников: {self.hud['conduits']} из 4.")
            if "life_support" in self.hud:
                lines.append(f"Жизнеобеспечение: {self.hud['life_support']}%.")
            recent = self.rows[-1] if self.rows else None
            if recent:
                minutes = max(0, int((recent["ended"] - recent["started"]) / 60))
                lines.append(
                    f"Последний забег: {'завершён' if recent['result'] == 'mission_complete' else 'провален'}, {minutes} мин, раунд {recent['round'] or 'не прочитан'}."
                )
                if recent.get("relics_seen"):
                    lines.append(
                        "На результатах замечены: "
                        + ", ".join(recent["relics_seen"])
                        + ". Количество не подтверждено; счётчик цели не изменён."
                    )
            today = time.strftime("%Y-%m-%d")
            runs = [
                r
                for r in self.rows
                if time.strftime("%Y-%m-%d", time.localtime(r["ended"])) == today
            ]
            if runs:
                lines.append(
                    f"Сегодня замечено завершённых миссий: {sum(r['result'] == 'mission_complete' for r in runs)}. Всего забегов с результатом: {len(runs)}."
                )
            if self.error:
                lines.append("Журнал не сохранён: " + self.error[:120])
            return (
                "\n".join(lines)
                or "Жду два читаемых кадра задания Warframe. Забеги и показатели появятся автоматически."
            )

    def snapshot(self):
        with self.lock:
            return {
                "summary": self.summary(),
                "runs": copy.deepcopy(self.rows[-20:]),
                "active": bool(self.active),
                "error": self.error,
            }
