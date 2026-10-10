"""Run from 'Scripts and bridges'. Replace refresh() with your bot integration."""

import os
from bridge_sdk import commands, emit


def main():
    # Read the optional key without logging it. Never embed it in source code.
    token = os.getenv("KREST_SERVICE_TOKEN", "")
    emit("ready", "Пример моста подключён", capabilities=["refresh"])
    for message in commands():
        if message["command"] == "refresh":
            emit("status", "Ключ задан" if token else "Работа без ключа")
            emit("notification", "Проверка моста: команда получена, уведомление доставлено")


if __name__ == "__main__":
    main()
