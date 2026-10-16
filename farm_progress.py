"""Credit observed Warframe rewards once, only after the user confirms quantity."""

import time


def credit(journal, store, name, quantity):
    quantity = int(quantity)
    if not 1 <= quantity <= 100:
        raise ValueError("Укажите количество от 1 до 100 за последний забег.")
    with journal.lock:
        run = dict(journal.rows[-1]) if journal.rows else {}
    if run.get("result") != "mission_complete" or time.time() - run.get("ended", 0) > 3600:
        raise ValueError("Нет результата завершённого забега за последний час.")
    query = name.strip().casefold()
    found = [
        v
        for v in run.get("relics_seen", [])
        if v.casefold() == query or v.casefold().startswith(query + " ")
    ]
    if len(found) != 1:
        raise ValueError(
            "Уточните одну реликвию с экрана наград: " + ", ".join(run.get("relics_seen", []))
        )
    relic = found[0]
    key = f"{run['started']}:{run['ended']}:{relic}"
    with store.edit() as data:
        receipts = data.setdefault("farm_credits", [])
        if any(r["id"] == key for r in receipts):
            raise ValueError("Эта добыча уже учтена. Повторного начисления нет.")
        goals = [
            g
            for g in data["goals"]
            if not g.get("done")
            and g["name"].casefold().strip()
            in (
                relic.casefold(),
                relic.split()[0].casefold(),
                "реликвии " + relic.split()[0].casefold(),
            )
        ]
        if len(goals) > 1:
            raise ValueError(
                "Несколько целей подходят этой реликвии. Оставьте одну активную цель или уточните прогресс вручную."
            )
        for goal in goals:
            goal["progress"] = min(goal["target"], goal.get("progress", 0) + quantity)
            goal["done"] = goal["progress"] >= goal["target"]
        receipts.append(
            {
                "id": key,
                "name": relic,
                "quantity": quantity,
                "time": time.time(),
                "minutes": max(0, (run["ended"] - run["started"]) / 60),
                "goal": goals[0]["id"] if goals else "",
            }
        )
        del receipts[:-300]
    return f"Учтено: {relic} × {quantity}." + (
        " Прогресс цели обновлён."
        if goals
        else " Совпадающей активной цели нет; добыча записана в отчёт."
    )


def summary(journal, store):
    data = store.snapshot("farm_credits", "goals")
    rows = data.get("farm_credits", [])
    today = time.strftime("%Y-%m-%d")
    recent = [r for r in rows if time.strftime("%Y-%m-%d", time.localtime(r["time"])) == today]
    lines = [journal.summary()]
    if recent:
        lines.append(
            "Подтверждено сегодня: " + str(sum(r["quantity"] for r in recent)) + " реликвий."
        )
    for goal in [g for g in data["goals"] if not g.get("done")][:4]:
        left = max(0, goal["target"] - goal.get("progress", 0))
        line = f"{goal['name']}: {goal.get('progress', 0)}/{goal['target']}, осталось {left}."
        credited = [r for r in recent if r["goal"] == goal["id"]]
        quantity = sum(r["quantity"] for r in credited)
        # Several relic types can be credited from the same mission.
        minutes = sum({r["id"].rsplit(":", 1)[0]: r["minutes"] for r in credited}.values())
        if quantity and minutes > 0:
            line += f" По подтверждённым забегам: примерно {round(left * minutes / quantity)} мин. Это оценка, награды случайны."
        lines.append(line)
    lines.append(
        "«Подтверди добычу акси количество один» — учесть реликвию последнего забега. Повторное подтверждение не дублирует прогресс."
    )
    return "\n".join(lines)
