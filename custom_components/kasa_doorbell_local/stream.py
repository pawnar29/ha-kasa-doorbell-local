"""Self-contained video pipeline — no go2rtc, no Frigate, no external binaries but ffmpeg.

Why this exists: the camera speaks a bespoke transport (HTTPS on a nonstandard port, self-signed
cert, legacy TLS ciphers, HTTP Basic auth, chunked transfer encoding wrapping a
``multipart/x-mixed-replace`` stream of H.264 + G.711 parts). Nothing in Home Assistant can open
that. go2rtc's ``kasa`` driver can — but HA ships a REDUCED go2rtc build with no kasa module
compiled in (verified: its /api/schemes lists no `kasa`), so relying on it means depending on a
full go2rtc elsewhere, e.g. Frigate's.

So we do it ourselves:

    camera  --(TLS multipart)-->  KasaStreamReader  --(H.264)-->  ffmpeg  --(MPEG-TS)-->  HA

ffmpeg is guaranteed present in every HA install, and HA's stream component opens MPEG-TS happily.
The same reader serves LIVE and RECORDED video — the only difference is the URL:

    live      :19443  /https/stream/mixed?video=h264&audio=g711&resolution=hd
    playback  :17443  /vod/data/mixstream?…&start_time=<epoch>   (+ X-PLAYER-ID header)

Parsing here was validated against the real
device (it decoded genuine recorded footage).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import ssl
import uuid
from collections.abc import AsyncIterator

from .const import (
    DEFAULT_AUDIO_CODEC,
    DEFAULT_RESOLUTION,
    DEFAULT_VIDEO_CODEC,
    STREAM_PATH,
    VOD_PATH,
    VOD_PORT,
)

_LOGGER = logging.getLogger(__name__)

# The camera announces `boundary=data-boundary--`; used only as a fallback if the response
# header is missing it.
DEFAULT_BOUNDARY = b"--data-boundary--"
READ_CHUNK = 65536

# The camera announces its rate in `X-FrameRate` (15.0 on the KD110); this is the fallback.
DEFAULT_FRAME_RATE = 15.0
# `audio=g711` in the stream URL yields G.711 µ-law, 8 kHz mono, in ~480 B parts.
AUDIO_FORMAT = "mulaw"
AUDIO_SAMPLE_RATE = 8000
AUDIO_BITRATE = "64k"
# ffmpeg blocks on any input that never produces data, so give up on audio after this and
# mux video alone rather than stalling the whole stream.
AUDIO_GRACE_SECONDS = 6
# How many video parts may sit between the camera reader and ffmpeg's stdin. This is the
# slack that lets the reader keep pulling AUDIO parts while ffmpeg is momentarily not
# reading video; when it fills, the reader blocks and TCP back-pressure reaches the camera,
# which is the behaviour we want. ~7 KB per part, so this is a few MB at most.
VIDEO_QUEUE_PARTS = 512
# How long a viewer may stop reading before we treat it as gone rather than merely buffering.
# Generous on purpose: a browser that has buffered ahead legitimately goes quiet for a while.
SUBSCRIBER_STALL_TIMEOUT = 30
# Which containers get an audio track muxed in.
#
# LIVE (mpegts) HAS AUDIO. It goes to HA's `stream` component, which demuxes the TS with
# PyAV and republishes HLS — a two-track H.264+AAC TS is the ordinary case there.
# ⚠️ The blanket audio-off that used to live here was inherited from a path that NO LONGER
# EXISTS: back when RECORDINGS were streamed as an open-ended fMP4 straight into a <video>,
# the browser would not start a two-track stream and sat on a spinner. Recordings are now
# captured to a finished MP4 first (clips.py, which muxes its own AAC and does have sound),
# so that reason never applied to live in the first place.
# fMP4 stays audio-free: nothing streams that container to a browser any more.
AUDIO_CONTAINERS: frozenset[str] = frozenset({"mpegts"})
# ⚠️ READ THE DOCS BEFORE TOUCHING THIS. With two inputs ffmpeg buffers output so it can
# interleave them, and the DEFAULT bound is 10 SECONDS — which is what made recordings sit
# there before showing anything once audio was added.
# `-max_interleave_delta 0` does NOT mean "no delay". It means buffer WITHOUT LIMIT until
# every stream has a packet. Shipping 0 made ffmpeg withhold the whole clip and dump it on
# exit (measured: 36 KB out after the entire input, then 552 KB at once).
# A small POSITIVE value is the one that streams: 0.1 s bounds the wait to nothing anyone
# notices. Measured at 100000: 413 KB out over the same feed.
_INTERLEAVE_BOUND = ("-max_interleave_delta", "100000")
# Cut an MP4 fragment this often. The camera's keyframes are ~12 s apart, and a browser can
# only render whole fragments — so without this, playback shows nothing for ~12 s.
FRAGMENT_MICROSECONDS = 500_000
# Don't let ffmpeg hold finished packets in its output buffer — this is the latency knob that
# is actually safe, unlike -fflags nobuffer / -flags low_delay, which mute the mpegts muxer.
_FLUSH = ("-flush_packets", "1")


class StreamError(RuntimeError):
    """The camera stream could not be opened or was lost."""


class KasaStreamReader:
    """Reads the camera's multipart stream and yields H.264 payloads.

    Handles the two things that trip up naive clients: the body is HTTP **chunked**, and the
    multipart boundary comes from the response header (not the one in the app's source).
    """

    def __init__(
        self,
        host: str,
        email: str,
        secret: str,
        *,
        port: int,
        path: str,
        query: str,
        player_id: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._query = query
        self._player_id = player_id
        self._ssl = ssl_context
        self._auth = base64.b64encode(f"{email}:{secret}".encode()).decode()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._raw = b""      # bytes off the socket, still chunk-framed
        self._buf = b""      # dechunked body
        self._chunked = False
        self._boundary = DEFAULT_BOUNDARY
        self._eof = False
        self._frame_rate = DEFAULT_FRAME_RATE

    @property
    def frame_rate(self) -> float:
        """Frames per second, from the response's ``X-FrameRate`` header.

        Raw H.264 carries no timing, so this is the only thing that lets playback run at the
        right SPEED — the camera dumps recorded footage far faster than realtime.
        """
        return self._frame_rate

    async def open(self, timeout: float = 15) -> None:
        """Connect and consume the response headers."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port, ssl=self._ssl),
                timeout=timeout,
            )
        except Exception as err:  # noqa: BLE001
            raise StreamError(f"connect failed: {type(err).__name__}") from err

        extra = f"X-PLAYER-ID: {self._player_id}\r\n" if self._player_id else ""
        request = (
            f"GET {self._path}?{self._query} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            f"Authorization: Basic {self._auth}\r\n"
            f"{extra}"
            f"Accept: */*\r\n"
            f"Connection: keep-alive\r\n\r\n"
        )
        self._writer.write(request.encode())
        await self._writer.drain()

        while b"\r\n\r\n" not in self._raw:
            chunk = await asyncio.wait_for(self._reader.read(READ_CHUNK), timeout=timeout)
            if not chunk:
                raise StreamError("connection closed before headers")
            self._raw += chunk
        head, self._raw = self._raw.split(b"\r\n\r\n", 1)
        text = head.decode("utf-8", "replace")

        status = text.split("\r\n", 1)[0]
        if " 200" not in status:
            # 503 means the camera's single stream slot is already taken.
            raise StreamError(f"camera refused the stream: {status.strip()}")

        for line in text.split("\r\n"):
            low = line.lower()
            if low.startswith("transfer-encoding") and "chunked" in low:
                self._chunked = True
            if low.startswith("content-type") and "boundary=" in low:
                value = line.split("boundary=", 1)[1].strip().strip('"')
                self._boundary = b"--" + value.encode()
            if low.startswith("x-framerate"):
                try:
                    rate = float(line.split(":", 1)[1].strip())
                except (IndexError, ValueError):
                    continue
                if 1 <= rate <= 120:  # ignore nonsense rather than mistime the whole clip
                    self._frame_rate = rate
        _LOGGER.debug(
            "Stream open %s:%s%s (chunked=%s boundary=%s)",
            self._host, self._port, self._path, self._chunked,
            self._boundary.decode(),
        )

    def _pump(self) -> None:
        """Move bytes raw -> buf, undoing chunked framing."""
        if not self._chunked:
            self._buf += self._raw
            self._raw = b""
            return
        while True:
            idx = self._raw.find(b"\r\n")
            if idx < 0:
                return
            try:
                size = int(self._raw[:idx].split(b";")[0].strip(), 16)
            except ValueError:
                return
            if size == 0:
                self._eof = True
                self._raw = b""
                return
            end = idx + 2 + size + 2
            if len(self._raw) < end:
                return
            self._buf += self._raw[idx + 2: idx + 2 + size]
            self._raw = self._raw[end:]

    async def _fill(self) -> bool:
        if self._eof or self._reader is None:
            return False
        chunk = await self._reader.read(READ_CHUNK)
        if not chunk:
            self._eof = True
            return False
        self._raw += chunk
        return True

    async def _find(self, marker: bytes) -> int:
        while True:
            self._pump()
            idx = self._buf.find(marker)
            if idx >= 0:
                return idx
            if not await self._fill():
                return -1

    async def _have(self, size: int) -> bool:
        while len(self._buf) < size:
            self._pump()
            if len(self._buf) >= size:
                return True
            if not await self._fill():
                return False
        return True

    async def iter_parts(self) -> AsyncIterator[tuple[str, bytes]]:
        """Yield ``("video"|"audio", payload)`` as parts arrive.

        Video is H.264 Annex-B; audio is raw G.711 µ-law at 8 kHz. Both go to ffmpeg, on
        separate pipes, because they are separate elementary streams.
        """
        while True:
            idx = await self._find(self._boundary)
            if idx < 0:
                return
            self._buf = self._buf[idx + len(self._boundary):]
            idx = await self._find(b"\r\n\r\n")
            if idx < 0:
                return
            head, self._buf = self._buf[:idx], self._buf[idx + 4:]

            headers: dict[str, str] = {}
            for line in head.decode("utf-8", "replace").split("\r\n"):
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length", 0) or 0)
            if length and not await self._have(length):
                return
            body, self._buf = self._buf[:length], self._buf[length:]
            content_type = headers.get("content-type", "")
            if "h264" in content_type:
                yield "video", body
            elif "g711" in content_type or "audio" in content_type:
                yield "audio", body

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001 — already gone
                pass
            self._writer = None
        self._reader = None


def live_reader_args(host: str, port: int) -> tuple[int, str, str, None]:
    """(port, path, query, player_id) for the LIVE stream."""
    return (
        port,
        STREAM_PATH,
        f"video={DEFAULT_VIDEO_CODEC}&audio={DEFAULT_AUDIO_CODEC}"
        f"&resolution={DEFAULT_RESOLUTION}",
        None,
    )


def playback_reader_args(start_time: int) -> tuple[int, str, str, str]:
    """(port, path, query, player_id) for RECORDED playback from `start_time` (epoch s).

    Recorded video lives on its own port and path, and the seek is just a query param —
    verified against the device by decoding genuine past footage.
    """
    return (
        VOD_PORT,
        VOD_PATH,
        f"video={DEFAULT_VIDEO_CODEC}&resolution={DEFAULT_RESOLUTION}"
        f"&audio={DEFAULT_AUDIO_CODEC}&start_time={start_time}",
        str(uuid.uuid4()),
    )


class KasaStreamPipeline:
    """reader -> ffmpeg -> MPEG-TS, for HA's stream component to consume."""

    def __init__(
        self,
        reader: KasaStreamReader,
        ffmpeg_bin: str = "ffmpeg",
        label: str = "live",
        container: str = "mpegts",
    ) -> None:
        self._reader = reader
        self._ffmpeg_bin = ffmpeg_bin
        self._container = container
        self._proc: asyncio.subprocess.Process | None = None
        self._feeder: asyncio.Task | None = None
        self._video_writer: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._audio_watchdog: asyncio.Task | None = None
        self._audio_transport = None
        self._audio_seen = False
        self._audio_parts = 0
        self._audio_bytes = 0
        self._subscribers: list[asyncio.Queue] = []
        self._label = label
        # Byte counters at both ends of ffmpeg. Without these a stalled pipeline is
        # indistinguishable from a camera that accepted the connection and sent nothing.
        self._bytes_in = 0
        self._bytes_out = 0

    async def start(self) -> None:
        await self._reader.open()

        # The camera interleaves H.264 and G.711 in ONE multipart stream, but ffmpeg needs
        # them as two inputs. stdin carries video; a second pipe, inherited by the child,
        # carries audio — only for containers we mux audio into (see AUDIO_CONTAINERS).
        audio_args: tuple[str, ...] = ()
        audio_r = audio_w = None
        if self._container in AUDIO_CONTAINERS:
            audio_r, audio_w = os.pipe()
            os.set_inheritable(audio_r, True)
            audio_args = (
                *self._audio_input_args(), "-i", f"pipe:{audio_r}",
                *self._audio_filter_args(),
                "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._ffmpeg_bin,
                "-hide_banner", "-loglevel", "error",
                *self._video_input_args(), "-f", "h264", "-i", "pipe:0",
                *audio_args,
                "-c:v", "copy",
                *self._output_args(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Captured, not discarded: when the muxer refuses a stream it says so here,
                # and discarding it once cost a long debugging detour.
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(audio_r,) if audio_r is not None else (),
            )
        finally:
            if audio_r is not None:
                os.close(audio_r)  # the child owns its copy now

        if audio_w is not None:
            loop = asyncio.get_running_loop()
            self._audio_transport, _ = await loop.connect_write_pipe(
                asyncio.Protocol, os.fdopen(audio_w, "wb", 0)
            )
            self._audio_watchdog = asyncio.create_task(self._audio_watchdog_run())

        self._feeder = asyncio.create_task(self._feed())
        self._pump_task = asyncio.create_task(self._broadcast())
        self._stderr_task = asyncio.create_task(self._log_stderr())

    async def _audio_watchdog_run(self) -> None:
        """Close the audio pipe if the camera never sends any.

        ffmpeg waits on every declared input, so a silent audio pipe would stall the whole
        mux and produce no video at all. Giving it EOF lets it carry on video-only.
        """
        try:
            await asyncio.sleep(AUDIO_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        if not self._audio_seen:
            _LOGGER.debug(
                "%s: no audio after %ss — muxing video only",
                self._label, AUDIO_GRACE_SECONDS,
            )
            self._close_audio()

    def _close_audio(self) -> None:
        if self._audio_transport is not None:
            try:
                self._audio_transport.close()
            except Exception:  # noqa: BLE001 — already gone
                pass
            self._audio_transport = None

    def _video_input_args(self) -> tuple[str, ...]:
        """How to timestamp the incoming raw H.264 — and it differs by container.

        Raw H.264 carries no timestamps and a muxer refuses to write without them
        ("first pts and dts value must be set").

        * LIVE / mpegts: stamp by ARRIVAL TIME. That is the correct semantics for a live
          feed, and it is also the only thing that works — the mpegts muxer still rejects
          `-r`-derived timing with that same error (re-verified against ffmpeg 7.1).
        * PLAYBACK / mp4: derive timing from the FRAME RATE instead. The camera dumps
          recorded footage at ~2.5x realtime, so arrival-time stamps made every clip play
          2.5x too fast. Frame-rate timing is independent of how fast the bytes turn up, so
          the clip runs at true speed no matter how the camera paces delivery.
        """
        if self._container == "mp4":
            return ("-r", f"{self._reader.frame_rate:g}")
        return ("-use_wallclock_as_timestamps", "1")

    def _audio_input_args(self) -> tuple[str, ...]:
        """G.711 µ-law, 8 kHz mono — the `audio=g711` the stream URL asks for.

        Raw µ-law has an implicit sample rate, so its timing is self-describing; it only
        needs arrival stamps in the live case, to share a clock with the video.
        """
        args = ("-f", AUDIO_FORMAT, "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1")
        if self._container == "mp4":
            return args
        return ("-use_wallclock_as_timestamps", "1", *args)

    def _audio_filter_args(self) -> tuple[str, ...]:
        """Rebuild a continuous audio timeline before encoding. LIVE ONLY.

        ⚠️ Without this, live audio is silently DROPPED ENTIRELY — the TS declares an AAC
        stream and carries zero AAC packets. The camera delivers audio in clumps, and
        `-use_wallclock_as_timestamps` stamps each arriving chunk with its ARRIVAL time; a
        clump of 60 ms parts landing inside 40 ms puts the sample clock permanently ahead of
        the arrival clock, so every later frame looks "backward in time" to the encoder
        (ffmpeg says exactly that) and is discarded. It never recovers on its own.

        `aresample=async` stretches or pads to keep one continuous timeline, which is what
        the encoder needs. `first_pts=0` anchors the start instead of inheriting the jitter.
        Playback does not need this: it has no wallclock stamps to fight with.
        """
        if self._container != "mpegts":
            return ()
        return ("-af", "aresample=async=1:min_hard_comp=0.100000:first_pts=0")

    def _output_args(self) -> tuple[str, ...]:
        """Container args. MPEG-TS feeds HA's `stream`; fMP4 feeds a browser <video> directly.

        The fragmented-MP4 flags are what let a <video> element start rendering an
        open-ended HTTP response: `empty_moov` emits a header with no sample table up front,
        `frag_keyframe` starts a new fragment at every keyframe, and `default_base_moof`
        makes each fragment self-contained. Without them the muxer would need to seek back
        and rewrite the header at the end, which a pipe cannot do.
        """
        if self._container == "mp4":
            return (
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                # ⚠️ `-frag_duration` is LOAD-BEARING, not a tweak. This camera's keyframe
                # interval is ~12 SECONDS (measured: ffmpeg emitted only 1231 B — the bare
                # header — after 929 KB of input, then trailed input by a steady ~1.5 MB).
                # A browser cannot render a fragment until it is complete, so keyframe-only
                # fragments meant ~12 s of nothing before the first frame. Cutting on time
                # instead makes fragments ~0.5 s. Verified in a real browser against a
                # 12 s-GOP stream.
                "-frag_duration", str(FRAGMENT_MICROSECONDS),
                *_INTERLEAVE_BOUND, *_FLUSH, "-f", "mp4", "pipe:1",
            )
        # NOTE: -fflags nobuffer / -flags low_delay were tried for latency and BREAK the
        # mpegts muxer (it never emits anything).
        # -flush_packets was tried here too and made LIVE stutter after a few seconds: it
        # splits output into many small writes, which filled the fan-out queue far sooner.
        # Live is not latency-critical enough to be worth that; leave it off.
        if self._container in AUDIO_CONTAINERS:
            # ⚠️ REQUIRED once there are two inputs, and easy to miss because the one-input
            # form never needed it: ffmpeg's default interleave bound is 10 SECONDS of
            # buffering while it pairs audio with video. Measured on a drip-fed synthetic
            # h264+mulaw feed (ffmpeg 7.1): 73% of the output was written while input was
            # still arriving with this bound, against 45% on the default and 47% at 0.
            # See _INTERLEAVE_BOUND — the value must be small and POSITIVE, never 0.
            return (*_INTERLEAVE_BOUND, "-f", "mpegts", "pipe:1")
        return ("-f", "mpegts", "pipe:1")

    async def _log_stderr(self) -> None:
        """Surface ffmpeg's own complaints."""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while line := await self._proc.stderr.readline():
                _LOGGER.warning(
                    "ffmpeg (%s): %s", self._label, line.decode("utf-8", "replace").strip()
                )
        except Exception:  # noqa: BLE001 — process went away
            pass

    async def _write_video(self, queue: asyncio.Queue) -> None:
        """Write queued video to ffmpeg's stdin. The ONLY place that awaits drain()."""
        assert self._proc is not None and self._proc.stdin is not None
        try:
            while (payload := await queue.get()) is not None:
                self._proc.stdin.write(payload)
                await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Video writer stopped: %s", err)

    async def _feed(self) -> None:
        """Read camera parts and dispatch them. MUST NOT block on ffmpeg — see below.

        ⚠️ Video is handed to a separate writer task rather than written here. With TWO
        inputs, ffmpeg stops reading stdin while it waits for an audio packet to advance
        interleaving; when this loop also owned the awaited `drain()`, that wait blocked the
        loop, so the next AUDIO part — which is what ffmpeg was waiting for, and which
        arrives on this same multipart connection — was never delivered. The two inputs
        deadlocked each other and the whole pipeline collapsed to a trickle.

        Measured against the real pipeline with a synthetic camera: 167 parts/s with audio
        off, 1.8 parts/s with audio on and a single feeder — matching the ~2 parts/s seen on
        the device. Audio writes go straight to the transport, which buffers rather than
        blocking, so the reader now always keeps draining the camera socket.
        """
        assert self._proc is not None and self._proc.stdin is not None
        parts = 0
        video_q: asyncio.Queue = asyncio.Queue(maxsize=VIDEO_QUEUE_PARTS)
        self._video_writer = asyncio.create_task(self._write_video(video_q))
        try:
            async for kind, payload in self._reader.iter_parts():
                if kind == "audio":
                    self._audio_seen = True
                    self._audio_parts += 1
                    self._audio_bytes += len(payload)
                    if self._audio_transport is not None:
                        self._audio_transport.write(payload)
                    continue
                await video_q.put(payload)
                parts += 1
                self._bytes_in += len(payload)
                if parts in (1, 10, 100) or parts % 500 == 0:
                    _LOGGER.debug(
                        "%s: fed %s video part(s) %s B / %s audio part(s) %s B"
                        " -> %s B out",
                        self._label, parts, self._bytes_in,
                        self._audio_parts, self._audio_bytes, self._bytes_out,
                    )
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Stream feeder stopped: %s", err)
        else:
            _LOGGER.debug(
                "%s: camera closed the connection after %s part(s), %s B",
                self._label, parts, self._bytes_in,
            )
        finally:
            self._close_audio()
            with contextlib.suppress(asyncio.QueueFull):
                video_q.put_nowait(None)   # sentinel: let the writer finish and exit
            if self._video_writer:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(self._video_writer, timeout=5)
                self._video_writer = None
            if self._proc and self._proc.stdin and not self._proc.stdin.is_closing():
                try:
                    self._proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass

    def add_subscriber(self) -> asyncio.Queue:
        """Register another consumer of this stream.

        The camera permits ONE connection, so extra viewers (HA opens the URL more than once
        — preview, retries) must share the single pipeline rather than each opening their own.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.append(queue)
        return queue

    def remove_subscriber(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    async def _broadcast(self) -> None:
        """Read ffmpeg's output once and fan it out to every subscriber."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                chunk = await self._proc.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                self._bytes_out += len(chunk)
                for queue in list(self._subscribers):
                    if not queue.full():
                        queue.put_nowait(chunk)
                        continue
                    # ⚠️ NEVER skip a chunk: dropping bytes from the middle of an MPEG-TS or
                    # fMP4 stream corrupts it, and the viewer plays a few seconds then lags.
                    # But do not hang up on the viewer either — a browser that has buffered
                    # enough simply stops reading for a while, which is normal, and cutting
                    # it off there means playback never starts at all.
                    # WAIT instead. Blocking here stops us draining ffmpeg, which blocks the
                    # feeder, which lets TCP back-pressure reach the camera — the whole
                    # pipeline just paces itself to the slowest viewer. Only a viewer that
                    # is genuinely gone gets disconnected.
                    try:
                        await asyncio.wait_for(
                            queue.put(chunk), timeout=SUBSCRIBER_STALL_TIMEOUT
                        )
                    except (TimeoutError, asyncio.TimeoutError):
                        _LOGGER.debug(
                            "%s: subscriber stalled for %ss; disconnecting it",
                            self._label, SUBSCRIBER_STALL_TIMEOUT,
                        )
                        self.remove_subscriber(queue)
                        with contextlib.suppress(asyncio.QueueFull):
                            queue.put_nowait(b"")   # sentinel: end of stream
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Broadcast ended: %s", err)
        finally:
            for queue in list(self._subscribers):
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(b"")     # sentinel: end of stream

    async def iter_ts(self, queue: asyncio.Queue) -> AsyncIterator[bytes]:
        """Yield this subscriber's share of the MPEG-TS."""
        while True:
            chunk = await queue.get()
            if not chunk:
                return
            yield chunk

    async def stop(self) -> None:
        """Tear everything down — important: it frees the camera's single stream slot."""
        _LOGGER.debug(
            "%s: stopping (video %s B, audio %s B in %s part(s) -> %s B of %s)",
            self._label, self._bytes_in, self._audio_bytes, self._audio_parts,
            self._bytes_out, self._container,
        )
        self._close_audio()
        for task in (
            self._pump_task, self._feeder, self._stderr_task, self._audio_watchdog,
            self._video_writer,
        ):
            if task:
                task.cancel()
        self._pump_task = self._stderr_task = self._audio_watchdog = None
        if self._feeder:
            try:
                await self._feeder
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._feeder = None
        await self._reader.close()
        if self._proc:
            if self._proc.returncode is None:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await self._proc.wait()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
