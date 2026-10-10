"""Microphone selection and streaming conversion without loading speech models."""

from array import array
import sys
import time
import warnings

LOOPBACK_NAMES = ("stereo mix", "стерео микшер", "what u hear", "loopback", "monitor of")


def input_candidates(sd, preferred=""):
    """Prefer explicit input, system default, then physical microphones. Never use output-only devices."""
    preferred = str(preferred).strip()
    # A minimal injected audio backend is useful in headless tests.
    if not hasattr(sd, "query_devices"):
        return [
            {
                "index": int(preferred) if preferred.isdigit() else preferred or None,
                "name": preferred or "Системный микрофон",
                "default_samplerate": 16000,
                "max_input_channels": 1,
            }
        ]
    devices = [
        dict(info, index=i)
        for i, info in enumerate(sd.query_devices())
        if info.get("max_input_channels", 0) > 0
    ]
    try:
        default = int(sd.default.device[0])
    except (AttributeError, ValueError, TypeError, IndexError):
        default = -1

    def score(item):
        name = item["name"].casefold()
        explicit = bool(preferred) and (
            str(item["index"]) == preferred
            or name == preferred.casefold()
            or not preferred.isdigit()
            and preferred.casefold() in name
        )
        physical = not any(part in name for part in LOOPBACK_NAMES)
        return (
            not explicit,
            not physical,
            item["index"] != default,
            not any(part in name for part in ("microphone", "микрофон", "headset", "гарнитур")),
            item["index"],
        )

    # System audio is not a microphone: only use a named loopback by explicit choice.
    return sorted((item for item in devices if not score(item)[1] or not score(item)[0]), key=score)


class PCMConverter:
    def __init__(self, rate, target=16000, channels=1):
        self.rate, self.target, self.channels = rate, target, channels
        self.state = None
        self.position = 0.0
        self.previous = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                import audioop
            self.audioop = audioop
        except ImportError:
            self.audioop = None

    def convert(self, data):
        raw = bytes(data)
        if self.audioop:
            if self.channels == 2:
                raw = self.audioop.tomono(raw, 2, 0.5, 0.5)
            if self.rate != self.target:
                raw, self.state = self.audioop.ratecv(raw, 2, 1, self.rate, self.target, self.state)
            return raw
        values = array("h")
        values.frombytes(raw)
        if sys.byteorder != "little":
            values.byteswap()
        if self.channels == 2:
            values = array(
                "h", (int((values[i] + values[i + 1]) / 2) for i in range(0, len(values) - 1, 2))
            )
        if self.rate == self.target:
            if sys.byteorder != "little":
                values.byteswap()
            return values.tobytes()
        points = self.previous + list(values)
        result = array("h")
        while self.position + 1 < len(points):
            i = int(self.position)
            fraction = self.position - i
            result.append(round(points[i] * (1 - fraction) + points[i + 1] * fraction))
            self.position += self.rate / self.target
        consumed = max(0, len(points) - 1)
        self.position -= consumed
        self.previous = points[-1:]
        if sys.byteorder != "little":
            result.byteswap()
        return result.tobytes()


def open_microphone(sd, preferred, callback, target_rate=16000):
    candidates = input_candidates(sd, preferred)
    errors = []
    for info in candidates:
        rates = list(
            dict.fromkeys(
                [target_rate, int(info.get("default_samplerate", target_rate)), 48000, 44100]
            )
        )
        for rate in rates:
            if rate <= 0:
                continue
            for channels in (1, 2) if info.get("max_input_channels", 1) >= 2 else (1,):
                stream = None
                try:
                    if hasattr(sd, "check_input_settings"):
                        sd.check_input_settings(
                            device=info["index"], samplerate=rate, channels=channels, dtype="int16"
                        )
                    converter = PCMConverter(rate, target_rate, channels)

                    def capture(data, frames, timing, status, converter=converter):
                        raw = converter.convert(data)
                        if raw:
                            callback(raw, len(raw) // 2, timing, status)

                    stream = sd.RawInputStream(
                        device=info["index"],
                        samplerate=rate,
                        channels=channels,
                        dtype="int16",
                        blocksize=max(160, int(rate * 0.05)),
                        callback=capture,
                    )
                    stream.start()
                    selected = dict(
                        info,
                        input_rate=rate,
                        target_rate=target_rate,
                        channels=channels,
                        connected_at=time.monotonic(),
                    )
                    return stream, selected
                except Exception as exc:
                    errors.append(info["name"] + ": " + str(exc))
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
    detail = errors[-1][:300] if errors else "Windows не сообщает доступных устройств ввода"
    raise RuntimeError(
        "Не удалось открыть микрофон. Проверьте подключение и разрешение доступа к микрофону. "
        + detail
    )


def refresh_idle_backend(sd):
    """PortAudio caches device lists on Windows. Caller must have closed all owned streams."""
    terminate, initialize = getattr(sd, "_terminate", None), getattr(sd, "_initialize", None)
    if callable(terminate) and callable(initialize):
        terminate()
        initialize()
