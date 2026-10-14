"""Bound public information responses by bytes, time and cooperative cancellation."""

import time


def read_response(response, *, limit=2 * 1024 * 1024, seconds=8, stop=None):
    if stop is None:
        from app_state import get_state

        runtime = getattr(get_state(), "runtime", None)
        stop = getattr(runtime, "command_stop", None)
    deadline = time.monotonic() + seconds
    # read1 does one underlying receive instead of waiting for a full block
    # from a server sending tiny pieces. Socket timeouts still bound each read.
    read = getattr(response, "read1", response.read)
    chunks, size = [], 0
    while True:
        if stop is not None and stop.is_set():
            raise TimeoutError("Запрос отменён")
        if time.monotonic() >= deadline:
            raise TimeoutError("Сервис отвечает слишком медленно")
        chunk = read(min(65536, limit + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise ValueError("Ответ сервиса превышает допустимый размер")
        chunks.append(chunk)


def read_requests_response(response, *, limit, seconds=25):
    raw = getattr(response, "raw", None)
    if callable(getattr(raw, "read1", None)):
        from types import SimpleNamespace

        read = lambda size: raw.read1(size, decode_content=True)
        from urllib3.exceptions import HTTPError

        try:
            return read_response(
                SimpleNamespace(read=read, read1=read), limit=limit, seconds=seconds
            )
        except HTTPError as exc:
            raise OSError("Не удалось прочитать ответ сервиса: " + str(exc)) from exc
    # Compatibility with older transports: tiny chunks ensure the deadline is
    # checked even when the peer continuously trickles data. Use bytearray so
    # this fallback does not allocate a list entry per byte.
    deadline, data = time.monotonic() + seconds, bytearray()
    for chunk in response.iter_content(1):
        if time.monotonic() >= deadline:
            raise TimeoutError("Сервис отвечает слишком медленно")
        if len(data) + len(chunk) > limit:
            raise ValueError("Ответ сервиса превышает допустимый размер")
        data.extend(chunk)
    return bytes(data)
