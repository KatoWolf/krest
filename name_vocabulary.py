"""An exact, catalog-backed vocabulary. No disk scans on the speech path."""

import re


def key(value):
    return re.sub(r"[^\w]+", " ", str(value).casefold().replace("ё", "е")).strip()


APP_NAMES = {
    "yandex music": ("яндекс", "яндекс музыка", "яндекс музыку", "ямузыка"),
    "яндекс музыка": ("яндекс", "яндекс музыку", "ямузыка"),
    "discord": ("дискорд", "дискорд приложение"),
    "telegram": ("телеграм", "телега", "телегу"),
    "google chrome": ("хром", "гугл хром"),
    "calculator": ("калькулятор",),
    "notepad": ("блокнот",),
    "obs studio": ("обс", "обс студио"),
    "steam": ("стим",),
}


class NameVocabulary:
    def __init__(self):
        self.rows = ()
        self.names = {}

    def update(self, applications, games, aliases=None):
        from skills.webo import POPULAR_GAME_ALIASES

        rows, names = [], {}

        def add(name, kind, variants, ident):
            command = ("открой программу " if kind == "app" else "запусти игру ") + name
            row = {"name": name, "kind": kind, "aliases": sorted(set(variants)), "command": command}
            rows.append(row)
            for variant in [name, *variants]:
                names.setdefault(key(variant), {})[(kind, ident)] = command

        for app in applications:
            name = app["name"]
            variants = [a for n, values in APP_NAMES.items() if key(n) == key(name) for a in values]
            variants += [a for a, n in (aliases or {}).items() if key(n) == key(name)]
            add(name, "app", variants, name.casefold())
        for game in games:
            variants = [
                a
                for n, values in POPULAR_GAME_ALIASES.items()
                if key(n) == key(game["name"])
                for a in values
            ]
            if str(game.get("appid")) == "730":
                variants += ["контра", "контру", "кс", "кс два", "кс 2", "ка эс два"]
            if str(game.get("appid")) == "230410":
                variants += ["варф", "варфрейм", "вар фрейм"]
            add(game["name"], "game", variants, str(game["appid"]))
        # Publish a complete immutable-by-convention snapshot in one assignment.
        self.rows, self.names = tuple(rows), names

    def resolve(self, text):
        match = re.fullmatch(
            r"(?:открой|запусти|включи) (?:(игру|программу|приложение) )?(.+)", text, re.I
        )
        if not match:
            return text
        choices = self.names.get(key(match[2]), {})
        if match[1]:
            kind = "game" if match[1] == "игру" else "app"
            choices = {k: v for k, v in choices.items() if k[0] == kind}
        values = set(choices.values())
        return next(iter(values)) if len(values) == 1 else text

    def phrases(self):
        return {
            verb + " " + name
            for name, choices in self.names.items()
            if len(choices) == 1 and len(name.split()) <= 6
            for verb in ("открой", "запусти")
        }
