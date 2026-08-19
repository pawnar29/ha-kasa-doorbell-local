"""Unit tests for the video pipeline's framing and ffmpeg argument construction.

No Home Assistant and no camera needed: the reader is fed a canned multipart response, and
the ffmpeg arguments are inspected rather than executed.

The ffmpeg flags here are ALL empirically determined and several are counter-intuitive, so
they are pinned: a "tidy-up" that swaps them silently produces either a 2.5x-speed clip or a
muxer that emits nothing at all.

Run directly (`python3 tests/test_stream.py`) or under pytest.
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

COMPONENT = (
    Path(__file__).resolve().parents[1] / "custom_components" / "kasa_doorbell_local"
)


def _load():
    pkg = "kasa_stream_stub"
    sys.modules[pkg] = types.ModuleType(pkg)
    sys.modules[pkg].__path__ = [str(COMPONENT)]
    mods = {}
    for name in ("const", "stream"):
        spec = importlib.util.spec_from_file_location(
            f"{pkg}.{name}", COMPONENT / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = pkg
        sys.modules[f"{pkg}.{name}"] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods["stream"]


st = _load()


def _run(coro):
    return asyncio.run(coro)


def _reader_fed(body: bytes, headers: bytes = b""):
    """A KasaStreamReader with its socket replaced by canned bytes."""
    reader = st.KasaStreamReader(
        "1.2.3.4", "a@b.c", "secret", port=1, path="/p", query="q"
    )

    class FakeSock:
        def __init__(self, data):
            self._data = data

        async def read(self, _n):
            data, self._data = self._data, b""
            return data

    head = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: multipart/x-mixed-replace; boundary=data-boundary--\r\n"
        + headers
        + b"\r\n"
    )
    reader._reader = FakeSock(body)
    reader._raw = head.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    # Consume the headers exactly as open() would.
    text = reader._raw.split(b"\r\n\r\n", 1)[0].decode()
    for line in text.split("\r\n"):
        low = line.lower()
        if low.startswith("content-type") and "boundary=" in low:
            reader._boundary = b"--" + line.split("boundary=", 1)[1].strip().encode()
        if low.startswith("x-framerate"):
            try:
                rate = float(line.split(":", 1)[1].strip())
            except (IndexError, ValueError):
                continue
            if 1 <= rate <= 120:
                reader._frame_rate = rate
    reader._raw = b""
    return reader


def _part(content_type: bytes, payload: bytes) -> bytes:
    return (
        b"--data-boundary--\r\n"
        b"Content-Type: " + content_type + b"\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    )


# ── tests ────────────────────────────────────────────────────────────────────
def test_video_and_audio_parts_are_split_by_content_type():
    body = (
        _part(b"video/x-h264", b"VVVV")
        + _part(b"audio/g711u", b"AAA")
        + _part(b"video/x-h264", b"VV")
    )
    reader = _reader_fed(body)

    async def collect():
        return [p async for p in reader.iter_parts()]

    assert _run(collect()) == [
        ("video", b"VVVV"),
        ("audio", b"AAA"),
        ("video", b"VV"),
    ]


def test_frame_rate_comes_from_the_header_with_a_sane_fallback():
    assert _reader_fed(b"", b"X-FrameRate: 20.0\r\n").frame_rate == 20.0
    assert _reader_fed(b"").frame_rate == st.DEFAULT_FRAME_RATE
    # Nonsense must not silently mistime an entire clip.
    assert _reader_fed(b"", b"X-FrameRate: 0\r\n").frame_rate == st.DEFAULT_FRAME_RATE
    assert _reader_fed(b"", b"X-FrameRate: wat\r\n").frame_rate == st.DEFAULT_FRAME_RATE


def _pipeline(container, frame_rate=15.0):
    reader = _reader_fed(b"")
    reader._frame_rate = frame_rate
    return st.KasaStreamPipeline(reader, "ffmpeg", "test", container)


def test_playback_is_timed_by_frame_rate_not_arrival():
    """The camera dumps VOD ~2.5x faster than realtime; arrival-time stamps baked that
    speed-up into every clip."""
    args = _pipeline("mp4", 15.0)._video_input_args()
    assert args == ("-r", "15")
    assert "-use_wallclock_as_timestamps" not in args
    # A camera reporting a different rate must be honoured, not rounded to a constant.
    assert _pipeline("mp4", 20.0)._video_input_args() == ("-r", "20")


def test_live_still_uses_wallclock_stamps():
    """mpegts rejects frame-rate-derived timing outright ("first pts and dts value must be
    set"), and arrival time is the right semantics for a live feed anyway."""
    assert _pipeline("mpegts")._video_input_args() == (
        "-use_wallclock_as_timestamps", "1",
    )


def test_audio_is_off():
    """Muxing works, but the browser will not start playing a two-track stream from this
    camera — it spins while loading. Video-only plays immediately, and playing beats sound."""
    assert st.AUDIO_CONTAINERS == frozenset()


def test_interleave_buffering_is_bounded_but_not_zero():
    """⚠️ `-max_interleave_delta 0` means buffer WITHOUT LIMIT, not "no buffering" — shipping
    0 made ffmpeg withhold a whole clip and dump it on exit. The default (10s) is also far
    too much. Only a small POSITIVE value streams."""
    args = _pipeline("mp4")._output_args()
    bound = int(args[args.index("-max_interleave_delta") + 1])
    assert 0 < bound <= 500_000


def test_audio_args_stay_correct_for_when_it_is_re_enabled():
    for container in ("mp4", "mpegts"):
        args = _pipeline(container)._audio_input_args()
        assert "-f" in args and "mulaw" in args
        assert "8000" in args
        assert args[args.index("-ac") + 1] == "1"
    # Live shares one clock across both inputs, or audio and video drift apart.
    assert _pipeline("mpegts")._audio_input_args()[:2] == (
        "-use_wallclock_as_timestamps", "1",
    )
    assert "-use_wallclock_as_timestamps" not in _pipeline("mp4")._audio_input_args()


def test_fmp4_output_is_streamable():
    """empty_moov + frag_keyframe put a playable header first; without them the muxer would
    have to seek back and rewrite it at the end, which a pipe cannot do."""
    args = _pipeline("mp4")._output_args()
    flags = args[args.index("-movflags") + 1]
    for flag in ("empty_moov", "frag_keyframe", "default_base_moof"):
        assert flag in flags
    assert args[-2:] == ("-f", "mp4") or args[-1] == "pipe:1"


def test_mpegts_output_stays_bare():
    """Live carries NO extra output flags. -fflags nobuffer / -flags low_delay make the
    mpegts muxer emit zero bytes, and -flush_packets made live stutter after a few seconds
    by splitting output into many small writes that overran the fan-out queue."""
    args = _pipeline("mpegts")._output_args()
    assert args == ("-f", "mpegts", "pipe:1")
    joined = " ".join(args)
    for flag in ("nobuffer", "low_delay", "flush_packets"):
        assert flag not in joined


def test_playback_fragments_on_time_not_only_on_keyframes():
    """The camera's keyframe interval is ~12 SECONDS. A browser renders whole fragments
    only, so keyframe-aligned fragments meant ~12s of blank before the first frame."""
    args = _pipeline("mp4")._output_args()
    assert args[args.index("-frag_duration") + 1] == str(st.FRAGMENT_MICROSECONDS)
    assert st.FRAGMENT_MICROSECONDS <= 1_000_000, "must be far below the ~12s GOP"
    assert "-flush_packets" in args


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as err:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(err).__name__}: {err}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
