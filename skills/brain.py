"""Voice access to the deterministic planner and explicitly deferred actions."""

from skill_manager import skill


@skill(
    keywords=["что ты сейчас делаешь", "состояние мозга", "поручения крест"],
    description="Назвать текущие действия и ожидающие поручения",
    category="Мозг",
)
def status(state):
    rt = state.runtime
    _, queued = rt.command_status()
    with rt._request_lock:
        running = " · ".join(text for lane, text in rt._running.items() if lane != "control")
    with rt.brain.lock:
        waiting = sum(row["status"] == "waiting" for row in rt.brain.goals.values())
    return f"В очереди команд: {queued}. Поручений ждут условия: {waiting}. " + (
        "Выполняются: " + running if running else "Сейчас свободна."
    )


@skill(
    keywords=["почему не выполнила", "объясни последнее действие", "отчёт о последней команде"],
    description="Показать шаги и подтверждение последнего действия",
    category="Мозг",
)
def explain(state):
    return state.runtime.brain.explain()


@skill(
    patterns=[r"(?:покажи план|продумай команду) (?P<command>.+)"],
    examples=["покажи план переключи трек и таймер на две минуты"],
    description="Разобрать действия без выполнения",
    category="Мозг",
)
def preview(command, state):
    plan = state.runtime.brain.preview(command)
    return "План без выполнения:\n" + "\n".join(
        f"{i}. {step['command']}" for i, step in enumerate(plan["steps"], 1)
    )


@skill(
    patterns=[
        r"(?P<condition>после игры|после катки|когда закончу играть|когда я отойду|когда компьютер свободен|когда компьютер освободится) (?P<command>.+)"
    ],
    examples=[
        "после игры таймер на две минуты",
        "когда компьютер свободен проверь подключения при простое",
    ],
    description="Сохранить однократное поручение на завершение игры или подтверждённый AFK",
    category="Мозг",
)
def defer_action(condition, command, state):
    return state.runtime.brain.add_goal(
        command,
        "after_game"
        if condition in {"после игры", "после катки", "когда закончу играть"}
        else "idle",
    )


@skill(
    keywords=["останови команды", "останови все команды"],
    description="Очистить очередь и остановить оставшиеся шаги выполняемых команд",
    category="Мозг",
)
def stop_actions(state):
    return state.runtime.brain.call("stop")


@skill(
    keywords=["отмени последнее поручение"],
    description="Отменить последнее активное поручение с условием",
    category="Мозг",
)
def cancel_deferred(state):
    brain = state.runtime.brain
    with brain.lock:
        row = max(brain.goals.values(), key=lambda g: g["created"], default=None)
    return brain.cancel_goal(row["id"]) if row else "Активных поручений с условием нет."
