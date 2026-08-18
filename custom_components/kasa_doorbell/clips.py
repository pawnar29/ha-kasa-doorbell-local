"""Capture a recording to a COMPLETE MP4 file, then serve it as an ordinary video.

Why not stream it. A recording is a finite asset, but serving it as an open-ended HTTP
response makes it behave like a live stream, and every browser heuristic then works against
us: fragments must be small enough to render, the muxer must not buffer, the player must
decide when it has "enough", and a two-track stream will not start until both tracks have
samples. That last one is why audio had to be dropped from the streaming path.

Capturing first removes all of it. ffmpeg writes a normal MP4 with a real duration and a
`moov` at the front, and `<video>` gets a file it can play instantly, seek within, and play
WITH SOUND. The cost is a wait before playback, and it is smaller than it sounds: the camera
delivers ~2.5x faster than realtime and we stop as soon as we have the clip's own length, so
a 20 s event takes about 10 s to fetch rather than the ~60 s the camera would keep sending.

Clips are cached in a temp directory for the lifetime of the HA process, so re-watching is
instant, with an LRU cap so a long browsing session cannot fill the disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import PLAYBACK_PREROLL_SECONDS
from .stream import (
    AUDIO_BITRATE,
    AUDIO_FORMAT,
    AUDIO_SAMPLE_RATE,
    KasaStreamReader,
    StreamError,
    playback_reader_args,
)

_LOGGER = logging.getLogger(__name__)

# Cap the cache so browsing a busy day cannot fill the disk. Clips run ~8 MB/min.
MAX_CACHE_BYTES = 1_500_000_000
# Never capture more than this, whatever the clip metadata claims — the camera stops sending
# at ~151 s anyway, and this bounds a bogus duration.
MAX_CLIP_SECONDS = 180
# A little past the end, so the event is not cut off by a rounding error.
CLIP_TAIL_SECONDS = 3

_CACHE = f"{__name__}_cache"


@dataclass
class Clip:
    """One capture: in progress, ready, or failed."""

    key: str
    path: Path
    wanted_frames: int
    frames: int = 0
    ready: bool = False
    error: str | None = None
    task: asyncio.Task | None = None
    used: float = field(default_factory=time.monotonic)

    @property
    def progress(self) -> int:
        """Percent captured, for the card to show while waiting."""
        if self.ready:
            return 100
        if not self.wanted_frames:
            return 0
        return min(99, int(100 * self.frames / self.wanted_frames))


class ClipCache:
    """Captures clips on demand and keeps the recent ones on disk."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._dir = Path(tempfile.mkdtemp(prefix="kasa_camera_clips_"))
        self._clips: dict[str, Clip] = {}

    def get(self, key: str) -> Clip | None:
        clip = self._clips.get(key)
        if clip is not None:
            clip.used = time.monotonic()
        return clip

    async def async_request(
        self, entry, seek: int, seconds: int
    ) -> Clip:
        """Return the clip for this seek point, starting a capture if needed.

        Concurrent requests for the same clip share one capture — the camera has a single
        stream slot, so a second fetch would only fight the first.
        """
        key = f"{entry.entry_id}_{seek}"
        if (clip := self.get(key)) is not None and clip.error is None:
            return clip

        seconds = max(1, min(seconds, MAX_CLIP_SECONDS))
        reader_args = playback_reader_args(seek)
        rate = 15.0  # replaced with the camera's own X-FrameRate once connected
        clip = Clip(
            key=key,
            path=self._dir / f"{key}.mp4",
            wanted_frames=int(seconds * rate),
        )
        self._clips[key] = clip
        clip.task = self.hass.async_create_task(
            self._async_capture(clip, entry, reader_args, seconds)
        )
        return clip

    async def _async_capture(self, clip: Clip, entry, reader_args, seconds: int) -> None:
        """Pull `seconds` of footage into a finished MP4."""
        port, path, query, player_id = reader_args
        coordinator = entry.runtime_data
        reader = KasaStreamReader(
            coordinator.host,
            entry.data["email"],
            coordinator.stream_password_b64,
            port=port,
            path=path,
            query=query,
            player_id=player_id,
            ssl_context=coordinator.ssl_context,
        )
        proc = None
        audio_w = None
        try:
            await reader.open()
            clip.wanted_frames = int(seconds * reader.frame_rate)

            audio_r, audio_w = os.pipe()
            os.set_inheritable(audio_r, True)
            ffmpeg = _ffmpeg_binary(self.hass)
            try:
                proc = await asyncio.create_subprocess_exec(
                    ffmpeg,
                    "-hide_banner", "-loglevel", "error",
                    # Frame-rate timing, not arrival time: the camera dumps footage ~2.5x
                    # faster than realtime and arrival stamps would bake that speed-up in.
                    "-r", f"{reader.frame_rate:g}",
                    "-f", "h264", "-i", "pipe:0",
                    "-f", AUDIO_FORMAT, "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1",
                    "-i", f"pipe:{audio_r}",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                    # A real file, so the muxer may rewrite the header at the end and put
                    # `moov` at the front — which is what lets the browser play and seek
                    # immediately. None of the fragmented-streaming flags are needed.
                    "-movflags", "+faststart",
                    "-y", str(clip.path),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    pass_fds=(audio_r,),
                )
            finally:
                os.close(audio_r)

            loop = asyncio.get_running_loop()
            audio_transport, _ = await loop.connect_write_pipe(
                asyncio.Protocol, os.fdopen(audio_w, "wb", 0)
            )
            audio_w = None  # the transport owns it now

            assert proc.stdin is not None
            async for kind, payload in reader.iter_parts():
                if kind == "audio":
                    audio_transport.write(payload)
                    continue
                proc.stdin.write(payload)
                await proc.stdin.drain()
                clip.frames += 1
                if clip.frames >= clip.wanted_frames:
                    break   # we have the whole event; stop early

            audio_transport.close()
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
            _, stderr = await proc.communicate()
            if stderr:
                _LOGGER.debug("ffmpeg (capture %s): %s", clip.key, stderr.decode()[:400])

            if not clip.path.is_file() or clip.path.stat().st_size == 0:
                raise StreamError("ffmpeg produced no file")
            clip.ready = True
            _LOGGER.info(
                "Captured %s frames to %s (%s KB)",
                clip.frames, clip.path.name, clip.path.stat().st_size // 1024,
            )
        except Exception as err:  # noqa: BLE001 — surfaced to the card
            clip.error = str(err) or type(err).__name__
            _LOGGER.error("Clip capture failed for %s: %s", clip.key, clip.error)
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        finally:
            if audio_w is not None:
                with contextlib.suppress(OSError):
                    os.close(audio_w)
            await reader.close()
            self._evict()

    def _evict(self) -> None:
        """Drop the least recently used clips once the cache grows past its cap."""
        ready = [c for c in self._clips.values() if c.ready and c.path.is_file()]
        total = sum(c.path.stat().st_size for c in ready)
        for clip in sorted(ready, key=lambda c: c.used):
            if total <= MAX_CACHE_BYTES:
                break
            total -= clip.path.stat().st_size
            clip.path.unlink(missing_ok=True)
            self._clips.pop(clip.key, None)

    def shutdown(self) -> None:
        for clip in self._clips.values():
            if clip.task and not clip.task.done():
                clip.task.cancel()
        shutil.rmtree(self._dir, ignore_errors=True)
        self._clips.clear()


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001 — fall back to PATH
        return "ffmpeg"


def async_get_cache(hass: HomeAssistant) -> ClipCache:
    """The process-wide clip cache."""
    if _CACHE not in hass.data:
        hass.data[_CACHE] = ClipCache(hass)
    return hass.data[_CACHE]


def clip_seconds(duration: int | None) -> int:
    """How much footage a clip of this length needs, including the seek preroll."""
    return (duration or 30) + PLAYBACK_PREROLL_SECONDS + CLIP_TAIL_SECONDS
