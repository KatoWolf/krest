from array import array
from types import SimpleNamespace

import pytest
from audio_devices import input_candidates, open_microphone, PCMConverter


class Audio:
    default = SimpleNamespace(device=(0, 9))

    def __init__(self):
        self.streams = []
        self.devices = [
            dict(name="Default microphone", max_input_channels=1, default_samplerate=48000),
            dict(name="USB headset", max_input_channels=2, default_samplerate=44100),
            dict(name="Speakers", max_input_channels=0, default_samplerate=48000),
            dict(name="Stereo Mix", max_input_channels=2, default_samplerate=48000),
        ]

    def query_devices(self):
        return self.devices

    def check_input_settings(self, **args):
        if args["device"] != 1 or args["samplerate"] != 44100 or args["channels"] != 2:
            raise ValueError("Unsupported format or disconnected device")

    def RawInputStream(self, **args):
        stream = SimpleNamespace(start=lambda: None, close=lambda: None, active=True, args=args)
        self.streams.append(stream)
        return stream


def test_input_selection_prefers_default_then_explicit_and_skips_output():
    sd = Audio()
    assert [r["index"] for r in input_candidates(sd)] == [0, 1]
    assert input_candidates(sd, "headset")[0]["index"] == 1
    assert input_candidates(sd, "missing")[0]["index"] == 0
    assert input_candidates(sd, "3")[0]["index"] == 3
    sd.default = SimpleNamespace(device=(3, 9))
    assert all(r["index"] != 3 for r in input_candidates(sd))


def test_fallback_native_stereo_is_converted_to_16k_mono():
    sd = Audio()
    received = []
    stream, info = open_microphone(
        sd, "0", lambda data, frames, *_: received.append((data, frames))
    )
    assert info["index"] == 1 and info["input_rate"] == 44100 and info["channels"] == 2
    raw = array("h", [1000, 3000] * 4410).tobytes()
    stream.args["callback"](raw, 4410, None, None)
    assert 1599 <= received[0][1] <= 1601
    assert set(array("h", received[0][0])) == {2000}


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
@pytest.mark.parametrize("fallback", [False, True])
def test_resampler_chunk_boundaries_do_not_lose_time(rate, fallback):
    convert = PCMConverter(rate)
    if fallback:
        convert.audioop = None
    chunks = [array("h", [1500] * (rate // 20)).tobytes() for _ in range(20)]
    result = b"".join(convert.convert(chunk) for chunk in chunks)
    assert abs(len(result) // 2 - 16000) <= 1
    assert set(array("h", result)) == {1500}


def test_failed_stream_is_closed_before_next_attempt():
    sd = Audio()
    closed = []
    sd.check_input_settings = lambda **kw: None

    def stream(**args):
        def start():
            raise RuntimeError("Device busy")

        return SimpleNamespace(start=start, close=lambda: closed.append(args["device"]))

    sd.RawInputStream = stream
    with pytest.raises(RuntimeError, match="Не удалось открыть микрофон"):
        open_microphone(sd, "", lambda *_: None)
    assert closed and 2 not in closed


def test_no_inputs_reports_access_and_connection_error():
    sd = Audio()
    sd.devices = []
    with pytest.raises(RuntimeError, match="разрешение доступа"):
        open_microphone(sd, "", lambda *_: None)
