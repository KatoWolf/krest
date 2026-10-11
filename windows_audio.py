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
    from pycaw.pycaw import AudioUtilities

    if AudioUtilities.GetSpeakers().id != device_id:
        raise ValueError("Windows не подтвердила смену основного устройства")
    return "Устройство воспроизведения изменено"
