"""Core Audio sessions. Change only an unambiguously named application."""

import os
from functools import wraps


def com_context(operation):
    @wraps(operation)
    def wrapped(*args, **kwargs):
        if os.name != "nt":
            raise ValueError("Управление звуком доступно в Windows")
        import comtypes

        comtypes.CoInitializeEx()
        try:
            return operation(*args, **kwargs)
        finally:
            comtypes.CoUninitialize()

    return wrapped


@com_context
def sessions():
    if os.name != "nt":
        raise ValueError("Микшер приложений доступен в Windows")
    from pycaw.pycaw import AudioUtilities
    import psutil

    rows = []
    for session in AudioUtilities.GetAllSessions():
        try:
            if session.Process:
                rows.append(
                    {
                        "name": session.Process.name(),
                        "pid": session.Process.pid,
                        "volume": round(session.SimpleAudioVolume.GetMasterVolume() * 100),
                        "muted": bool(session.SimpleAudioVolume.GetMute()),
                    }
                )
        except (OSError, AttributeError, psutil.Error):
            continue
    return rows


@com_context
def set_app_volume(name, percent):
    if not 0 <= int(percent) <= 100:
        raise ValueError("Громкость: 0–100%")
    if os.name != "nt":
        raise ValueError("Микшер приложений доступен в Windows")
    from pycaw.pycaw import AudioUtilities

    found = [
        s
        for s in AudioUtilities.GetAllSessions()
        if s.Process and s.Process.name().casefold() == name.casefold()
    ]
    if len(found) != 1:
        raise ValueError(
            "Нужна ровно одна аудиосессия программы. Сейчас найдено: " + str(len(found))
        )
    session = found[0].SimpleAudioVolume
    session.SetMasterVolume(int(percent) / 100, None)
    if abs(session.GetMasterVolume() * 100 - int(percent)) > 1:
        raise ValueError("Приложение не подтвердило новую громкость")
    return f"{name}: громкость {percent}%"


@com_context
def adjust_app_volume(name, delta, *, friendly=""):
    """Read and change the same COM session; never fall back to master volume."""
    from pycaw.pycaw import AudioUtilities
    from media_control import app_name

    found = []
    for session in AudioUtilities.GetAllSessions():
        if session.Process:
            process = session.Process.name()
            if (name and process.casefold() == name.casefold()) or (
                not name and friendly and app_name(process) == friendly
            ):
                found.append((session, process))
    if len(found) != 1:
        raise ValueError(
            "Не могу однозначно выбрать звук приложения; включите в нём воспроизведение. Найдено сессий: "
            + str(len(found))
        )
    session, process = found[0]
    control = session.SimpleAudioVolume
    level = max(0, min(100, round(control.GetMasterVolume() * 100) + int(delta)))
    control.SetMasterVolume(level / 100, None)
    if abs(control.GetMasterVolume() * 100 - level) > 1:
        raise ValueError("Приложение не подтвердило изменение громкости")
    return {
        "success": True,
        "message": f"{app_name(process)}: громкость {level}%.",
        "verification": "confirmed",
        "voice_context": {"process": process},
    }


@com_context
def output_devices():
    if os.name != "nt":
        raise ValueError("Устройства звука доступны в Windows")
    from pycaw.pycaw import AudioUtilities

    return [
        {"id": d.id, "name": d.FriendlyName}
        for d in AudioUtilities.GetAllDevices()
        if getattr(d.state, "value", d.state) == 1 and str(d.id).startswith("{0.0.0.")
    ]


@com_context
def set_default_output(device_id):
    if device_id not in {d["id"] for d in output_devices()}:
        raise ValueError("Выбранное устройство больше не доступно")
    return set_default_device(device_id)


@com_context
def set_default_device(device_id):
    from pycaw.pycaw import AudioUtilities

    available = {
        d.id for d in AudioUtilities.GetAllDevices() if getattr(d.state, "value", d.state) == 1
    }
    if device_id not in available:
        raise ValueError("Выбранное устройство больше не доступно")
    import ctypes
    import comtypes
    from ctypes import wintypes

    class PolicyConfig(comtypes.IUnknown):
        _iid_ = comtypes.GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [
            comtypes.COMMETHOD([], ctypes.HRESULT, name)
            for name in (
                "GetMixFormat",
                "GetDeviceFormat",
                "ResetDeviceFormat",
                "SetDeviceFormat",
                "GetProcessingPeriod",
                "SetProcessingPeriod",
                "GetShareMode",
                "SetShareMode",
                "GetPropertyValue",
                "SetPropertyValue",
            )
        ] + [
            comtypes.COMMETHOD(
                [],
                ctypes.HRESULT,
                "SetDefaultEndpoint",
                (["in"], wintypes.LPCWSTR, "device"),
                (["in"], wintypes.DWORD, "role"),
            ),
            comtypes.COMMETHOD([], ctypes.HRESULT, "SetEndpointVisibility"),
        ]

    policy = comtypes.CoCreateInstance(
        "{870af99c-171d-4f9e-af0d-e63df40c2bc9}", PolicyConfig, comtypes.CLSCTX_ALL
    )
    for role in range(3):
        policy.SetDefaultEndpoint(device_id, role)
    flow = 1 if str(device_id).startswith("{0.0.1.") else 0
    if AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(flow, 0).GetId() != device_id:
        raise ValueError("Windows не подтвердила смену основного устройства")
    return "Устройство звука изменено"


@com_context
def focus_sessions():
    from pycaw.pycaw import AudioUtilities

    rows = []
    for session in AudioUtilities.GetAllSessions():
        if session.Process:
            rows.append(
                {
                    "id": session.InstanceIdentifier,
                    "name": session.Process.name(),
                    "volume": session.SimpleAudioVolume.GetMasterVolume(),
                }
            )
    return rows


@com_context
def focus_set(ident, volume):
    return focus_set_many.__wrapped__([{"id": ident, "volume": volume}])


@com_context
def focus_set_many(changes):
    """Enumerate once for a profile, and do not write unchanged volumes."""
    from pycaw.pycaw import AudioUtilities
    import math

    targets = {row["id"]: float(row["volume"]) for row in changes}
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in targets.values()):
        raise ValueError("Некорректная громкость аудиосессии")
    if not targets:
        return
    for session in AudioUtilities.GetAllSessions():
        ident = session.InstanceIdentifier
        if ident in targets:
            volume = targets[ident]
            control = session.SimpleAudioVolume
            if abs(control.GetMasterVolume() - volume) > 0.005:
                control.SetMasterVolume(volume, None)
            if abs(control.GetMasterVolume() - volume) > 0.011:
                raise ValueError("Приложение не подтвердило громкость")
            targets.pop(ident)
            if not targets:
                return
    if targets:
        raise ValueError("Аудиосессия закрыта")
