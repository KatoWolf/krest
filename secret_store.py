"""Windows Credential Manager for application tokens; DPAPI for IPC bootstrap data."""

import base64
import ctypes
from ctypes import wintypes
import hashlib
import os

PREFIX = "@krest-vault:"
SECRET_KEYS = {
    "krest_bot_token",
    "discord_token",
    "discord_user_token",
    "telegram_api_hash",
    "steam_api_key",
}


def _target(scope, key):
    return "Krest/" + hashlib.sha256(str(scope).encode()).hexdigest()[:24] + "/" + key


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def store_secret(target, value):
    if os.name != "nt":
        raise OSError("Хранилище учётных данных доступно только в Windows")
    raw = value.encode("utf-8")
    if len(raw) > 2560:
        raise ValueError("Секрет слишком длинный для хранилища Windows")
    data = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = Credential(
        Type=1,
        TargetName=target,
        CredentialBlobSize=len(raw),
        CredentialBlob=data,
        Persist=2,
        UserName="Krest",
    )
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def read_secret(target):
    if os.name != "nt":
        raise OSError("Этот секрет сохранён в хранилище Windows исходного пользователя")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    output = ctypes.POINTER(Credential)()
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(Credential)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    if not api.CredReadW(target, 1, 0, ctypes.byref(output)):
        raise OSError("Секрет не найден в хранилище Windows. Введите его заново.")
    try:
        return ctypes.string_at(
            output.contents.CredentialBlob, output.contents.CredentialBlobSize
        ).decode("utf-8")
    finally:
        api.CredFree(output)


def hydrate(config, errors=None):
    for values in [config.get("api_keys", {}), *config.get("services", {}).values()]:
        for key, value in list(values.items()):
            if isinstance(value, str) and value.startswith(PREFIX):
                try:
                    values[key] = read_secret(value[len(PREFIX) :])
                except OSError:
                    values[key] = ""
                    if errors is not None:
                        errors.append(f"Введите заново ключ {key}: хранилище недоступно")
    return config


def protect_config(config, scope):
    import copy

    result = copy.deepcopy(config)
    if os.name != "nt":
        return result
    for key in SECRET_KEYS:
        value = result["api_keys"].get(key)
        if value:
            target = _target(scope, "api/" + key)
            store_secret(target, value)
            result["api_keys"][key] = PREFIX + target
    for ident, values in result.get("services", {}).items():
        if values.get("token"):
            target = _target(scope, "service/" + ident)
            store_secret(target, values["token"])
            values["token"] = PREFIX + target
    return result


class Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect_bootstrap(value, decrypt=False):
    if os.name != "nt":
        return value
    raw = base64.b64decode(value) if decrypt else value.encode()
    data = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, output = Blob(len(raw), data), Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        data = ctypes.string_at(output.data, output.length)
        return data.decode() if decrypt else base64.b64encode(data).decode()
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(output.data)
