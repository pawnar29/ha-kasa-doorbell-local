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
    args = _pipeline("mpegts")._video_input_args()
    assert args[-2:] == ("-use_wallclock_as_timestamps", "1")
    # Live also probes small, so the first byte is not held back by ffmpeg's 5 MB default.
    assert args[:4] == st._FAST_PROBE
    # Recordings keep ffmpeg's default probing.
    assert "-probesize" not in _pipeline("mp4")._video_input_args()


def test_live_has_audio_and_fmp4_does_not():
    """Live is a two-track TS handed to HA's `stream`, which is the ordinary case for it.
    fMP4 stays video-only: nothing streams that container to a browser any more — recordings
    are captured to a finished file by clips.py, which muxes its own audio."""
    assert st.AUDIO_CONTAINERS == frozenset({"mpegts"})


def test_interleave_buffering_is_bounded_but_not_zero():
    """⚠️ `-max_interleave_delta 0` means buffer WITHOUT LIMIT, not "no buffering" — shipping
    0 made ffmpeg withhold a whole clip and dump it on exit. The default (10s) is also far
    too much. Only a small POSITIVE value streams."""
    args = _pipeline("mp4")._output_args()
    bound = int(args[args.index("-max_interleave_delta") + 1])
    assert 0 < bound <= 500_000


def test_audio_input_args_describe_g711_mulaw():
    for container in ("mp4", "mpegts"):
        args = _pipeline(container)._audio_input_args()
        assert "-f" in args and "mulaw" in args
        assert "8000" in args
        assert args[args.index("-ac") + 1] == "1"
    # Live shares one clock across both inputs, or audio and video drift apart.
    live = _pipeline("mpegts")._audio_input_args()
    assert "-use_wallclock_as_timestamps" in live
    assert live[live.index("-use_wallclock_as_timestamps") + 1] == "1"
    assert "-use_wallclock_as_timestamps" not in _pipeline("mp4")._audio_input_args()


def test_reader_never_awaits_ffmpeg_directly():
    """⚠️ The reader loop must not own the awaited drain().

    With two inputs ffmpeg stops reading stdin while it waits for an audio packet; when the
    reader loop also awaited drain(), the next AUDIO part — arriving on the same multipart
    connection — was never delivered, so the inputs deadlocked. Measured against the real
    pipeline with a realtime synthetic camera offering 25 parts/s: 1.8 parts/s with one
    feeder, 23.7 with the writer split out. The device showed exactly 1.8-2.0.
    """
    import inspect

    # Matched on "stdin.drain" rather than "drain()" so the explanation in the docstring
    # does not trip the check.
    feed_src = inspect.getsource(st.KasaStreamPipeline._feed)
    assert "stdin.drain" not in feed_src, "the reader loop must not await ffmpeg's stdin"
    assert "video_q.put" in feed_src
    writer_src = inspect.getsource(st.KasaStreamPipeline._write_video)
    assert "stdin.drain" in writer_src, "the video writer is where drain() belongs"
    # Bounded, so a stalled ffmpeg still back-pressures the camera instead of buffering
    # without limit.
    assert 0 < st.VIDEO_QUEUE_PARTS <= 2048


def test_live_audio_gets_a_resampled_timeline():
    """⚠️ Load-bearing. Without the filter the camera's clumped audio delivery puts the
    sample clock permanently ahead of the wallclock stamps, every frame reads as "backward
    in time" to the encoder, and the TS ends up declaring an AAC stream with ZERO packets.
    Measured on the real device: 0 audio TS packets without it, 146 with it."""
    args = _pipeline("mpegts")._audio_filter_args()
    assert args[0] == "-af"
    assert "aresample=async=" in args[1]
    assert "first_pts=0" in args[1]
    # Playback has no wallclock stamps to fight with, so it must not pay for this.
    assert _pipeline("mp4")._audio_filter_args() == ()


def test_fmp4_output_is_streamable():
    """empty_moov + frag_keyframe put a playable header first; without them the muxer would
    have to seek back and rewrite it at the end, which a pipe cannot do."""
    args = _pipeline("mp4")._output_args()
    flags = args[args.index("-movflags") + 1]
    for flag in ("empty_moov", "frag_keyframe", "default_base_moof"):
        assert flag in flags
    assert args[-2:] == ("-f", "mp4") or args[-1] == "pipe:1"


def test_mpegts_carries_only_the_interleave_bound():
    """Live takes NO latency flags: -fflags nobuffer / -flags low_delay make the mpegts muxer
    emit zero bytes, and -flush_packets made live stutter after a few seconds by splitting
    output into many small writes that overran the fan-out queue.

    The interleave bound is the one exception, and it is mandatory rather than optional: with
    an audio input as well, ffmpeg's default is to buffer up to 10 s while pairing streams."""
    args = _pipeline("mpegts")._output_args()
    assert args == ("-max_interleave_delta", "100000", "-f", "mpegts", "pipe:1")
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
