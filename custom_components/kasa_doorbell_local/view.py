"""HTTP view that re-serves the camera as MPEG-TS, so HA's stream component can play it.

HA's stream component opens `stream_source()` with PyAV, which cannot speak the camera's
bespoke multipart transport. Rather than depend on a full go2rtc build (HA's bundled one has
no `kasa` driver), the integration converts the stream itself and serves it from here.

    /api/kasa_doorbell_local/live/<token>            live view
    /api/kasa_doorbell_local/playback/<token>/<ts>   recorded, from that epoch second

The URL carries an unguessable per-entry token; HA's stream component fetches these over
loopback, so nothing camera-related is exposed without it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .stream import (
    KasaStreamPipeline,
    KasaStreamReader,
    StreamError,
    live_reader_args,
    playback_reader_args,
)

_LOGGER = logging.getLogger(__name__)

# The URL carries an unguessable per-entry token instead of relying on HA signed paths,
# whose helper signature has changed across HA versions. HA's own stream component fetches
# these over loopback, so the token is the access control.
LIVE_URL = "/api/kasa_doorbell_local/live/{token}"
PLAYBACK_URL = "/api/kasa_doorbell_local/playback/{token}/{start_time}"
_TOKENS = f"{__name__}_tokens"


def async_get_token(hass: HomeAssistant, entry_id: str) -> str:
    """Stable random token identifying this entry's stream endpoints."""
    tokens = hass.data.setdefault(_TOKENS, {})
    if entry_id not in tokens:
        tokens[entry_id] = secrets.token_urlsafe(32)
    return tokens[entry_id]


def _async_local_url(hass: HomeAssistant, path: str) -> str:
    """Absolute loopback URL for one of our views.

    Everything that consumes these (HA's stream component, ffmpeg) runs inside HA, so going
    over 127.0.0.1 sidesteps external URLs, reverse proxies and DNS entirely.
    """
    scheme = "https" if hass.http.ssl_certificate else "http"
    return f"{scheme}://127.0.0.1:{hass.http.server_port}{path}"


def async_live_url(hass: HomeAssistant, entry_id: str) -> str:
    """Loopback URL serving this camera's LIVE video as MPEG-TS."""
    return _async_local_url(hass, LIVE_URL.format(token=async_get_token(hass, entry_id)))


def async_playback_path(hass: HomeAssistant, entry_id: str, start_time: int) -> str:
    """Root-relative path serving RECORDED video from `start_time` as fragmented MP4.

    Relative, not loopback: this one is handed to the USER'S BROWSER (the recordings card,
    and media source resolution), which obviously cannot reach 127.0.0.1. The browser
    resolves it against whatever origin it already reached HA on, so this works over LAN,
    a reverse proxy, or remote access without any URL configuration.
    """
    return PLAYBACK_URL.format(
        token=async_get_token(hass, entry_id), start_time=int(start_time)
    )


def _entry_for_token(hass: HomeAssistant, token: str) -> str | None:
    for entry_id, value in hass.data.get(_TOKENS, {}).items():
        if secrets.compare_digest(value, token):
            return entry_id
    return None


async def _serve(hass: HomeAssistant, entry_id: str, start_time: int | None):
    """Build the pipeline for one request. Returns (pipeline, error_message)."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or not hasattr(entry, "runtime_data"):
        return None, "unknown config entry"
    coordinator = entry.runtime_data
    secret = coordinator.stream_password_b64
    if not secret:
        return None, "no device secret"

    if start_time is None:
        port, path, query, player_id = live_reader_args(
            coordinator.host, coordinator.stream_port
        )
    else:
        port, path, query, player_id = playback_reader_args(start_time)

    from homeassistant.components.ffmpeg import get_ffmpeg_manager

    try:
        ffmpeg_bin = get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001 — fall back to PATH
        ffmpeg_bin = "ffmpeg"

    reader = KasaStreamReader(
        coordinator.host,
        entry.data["email"],
        secret,
        port=port,
        path=path,
        query=query,
        player_id=player_id,
        ssl_context=coordinator.ssl_context,
    )
    # Live feeds HA's `stream` component, which wants MPEG-TS. Playback goes straight to a
    # browser <video> in our own card, so it gets fragmented MP4 — no HLS in the middle.
    live = start_time is None
    label = "live" if live else f"playback@{start_time}"
    container = "mpegts" if live else "mp4"
    pipeline = KasaStreamPipeline(reader, ffmpeg_bin, label, container)
    # The camera keeps its single stream slot reserved for a few seconds after a client
    # goes away, so a 503 right after a teardown is usually transient.
    last = ""
    for attempt in range(4):
        try:
            await pipeline.start()
        except StreamError as err:
            last = str(err)
            await pipeline.stop()
            if "503" not in last or attempt == 3:
                return None, last
            await asyncio.sleep(1.5 * (attempt + 1))
            pipeline = KasaStreamPipeline(
                KasaStreamReader(
                    coordinator.host, entry.data["email"], secret,
                    port=port, path=path, query=query, player_id=player_id,
                    ssl_context=coordinator.ssl_context,
                ),
                ffmpeg_bin,
                label,
                container,
            )
            continue
        return pipeline, None
    return None, last


class KasaStreamView(HomeAssistantView):
    """Streams live camera video as MPEG-TS."""

    url = LIVE_URL
    name = "api:kasa_doorbell_local:live"
    requires_auth = False  # guarded by the unguessable per-entry token in the URL

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        entry_id = _entry_for_token(self.hass, token)
        if entry_id is None:
            raise web.HTTPNotFound()
        return await self._stream(request, entry_id, None)

    async def _stream(
        self, request: web.Request, entry_id: str, start_time: int | None
    ) -> web.StreamResponse:
        # The camera allows exactly ONE stream connection. Serialize starts, and never tear
        # down a pipeline that is already working just because another request arrived —
        # doing that made concurrent requests kill each other's stream.
        lock = self.hass.data.setdefault(_LOCKS, {}).setdefault(entry_id, asyncio.Lock())
        async with lock:
            running = self.hass.data.setdefault(_PIPELINES, {}).get(entry_id)
            # Reuse the running pipeline only when it is showing THE SAME thing, so extra
            # viewers share the camera's single connection (HA opens the URL more than once:
            # preview, retries). `start_time` is the identity: None is live, and two seeks to
            # different timestamps are different streams — joining across them would hand a
            # viewer someone else's footage.
            if running is not None and running[1] == start_time:
                pipeline = running[0]
                _LOGGER.debug(
                    "Joining the running %s stream for %s",
                    "live" if start_time is None else f"playback@{start_time}", entry_id,
                )
            else:
                if running is not None:
                    # ⚠️ PLAYBACK OUTRANKS LIVE. Both cannot have the camera's single slot,
                    # and HA's stream component retries the live URL every few seconds
                    # forever. Letting live win meant a recording was evicted ~10 s in — a
                    # 2-minute clip stopped dead after ~25 s of footage. So a recording may
                    # take the slot from live (the user asked for it by clicking), but never
                    # the other way round: the live view goes unavailable until the
                    # recording finishes.
                    if start_time is None and running[1] is not None:
                        _LOGGER.debug(
                            "Live refused for %s: playback@%s is using the camera",
                            entry_id, running[1],
                        )
                        raise web.HTTPServiceUnavailable(
                            text="a recording is playing; the camera has one stream slot"
                        )
                    await async_stop_pipeline(self.hass, entry_id)
                pipeline, error = await _serve(self.hass, entry_id, start_time)
                if pipeline is None:
                    _LOGGER.error("Cannot start stream for %s: %s", entry_id, error)
                    raise web.HTTPServiceUnavailable(text=error or "stream unavailable")
                self.hass.data[_PIPELINES][entry_id] = (pipeline, start_time)
            queue = pipeline.add_subscriber()
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "video/mp2t" if start_time is None else "video/mp4",
                # The length is unknown (the camera ends the clip when it feels like it) and
                # a browser must not try to Range-request into a live mux.
                "Cache-Control": "no-store",
                "Accept-Ranges": "none",
            },
        )
        await response.prepare(request)
        try:
            async for chunk in pipeline.iter_ts(queue):
                await response.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError):
            pass  # viewer went away
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Stream ended for %s: %s", entry_id, err)
        finally:
            pipeline.remove_subscriber(queue)
            # Only tear the camera connection down once the LAST viewer leaves — and
            # DEREGISTER BEFORE STOPPING. `stop()` awaits, and a request arriving during that
            # await used to find this pipeline still in the registry and join it, subscribing
            # to something that would never send another byte; its stream worker then hung
            # until PyAV's 30 s open timeout ("Immediate exit requested"). Deciding and
            # deregistering under the same lock the start path holds closes that window.
            async with lock:
                orphaned = pipeline.subscribers == 0
                if orphaned:
                    current = self.hass.data.get(_PIPELINES, {}).get(entry_id)
                    # Leave it alone if another request already replaced us in the slot.
                    if current is not None and current[0] is pipeline:
                        self.hass.data[_PIPELINES].pop(entry_id, None)
            if orphaned:
                await pipeline.stop()
        return response


class KasaPlaybackView(KasaStreamView):
    """Streams RECORDED video from the camera's SD card, seeked to a timestamp."""

    url = PLAYBACK_URL
    name = "api:kasa_doorbell_local:playback"

    async def get(  # type: ignore[override]
        self, request: web.Request, token: str, start_time: str
    ) -> web.StreamResponse:
        entry_id = _entry_for_token(self.hass, token)
        if entry_id is None:
            raise web.HTTPNotFound()
        try:
            when = int(start_time)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="start_time must be epoch seconds")
        return await self._stream(request, entry_id, when)


_PIPELINES = f"{__name__}_pipelines"
_LOCKS = f"{__name__}_locks"


async def async_stop_pipeline(hass: HomeAssistant, entry_id: str) -> None:
    """Stop any running pipeline for this entry — frees the camera's single stream slot."""
    running = hass.data.get(_PIPELINES, {}).pop(entry_id, None)
    if running is not None:
        await running[0].stop()


CLIP_URL = "/api/kasa_doorbell_local/clip/{token}/{start_time}"


def async_clip_path(hass: HomeAssistant, entry_id: str, start_time: int) -> str:
    """Root-relative URL of a CAPTURED clip — an ordinary, seekable MP4 file."""
    return CLIP_URL.format(
        token=async_get_token(hass, entry_id), start_time=int(start_time)
    )


class KasaClipView(HomeAssistantView):
    """Serves a captured recording as a plain MP4 file.

    `web.FileResponse` gives us Content-Length and Range support for free, which is what
    lets the browser show a real duration, seek, and play immediately — none of which is
    possible over the open-ended stream this replaced.
    """

    url = CLIP_URL
    name = "api:kasa_doorbell_local:clip"
    requires_auth = False  # guarded by the unguessable per-entry token in the URL

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, token: str, start_time: str):
        entry_id = _entry_for_token(self.hass, token)
        if entry_id is None:
            raise web.HTTPNotFound()
        from .clips import async_get_cache

        clip = async_get_cache(self.hass).get(f"{entry_id}_{start_time}")
        if clip is None or not clip.ready or not clip.path.is_file():
            # The card prepares a clip over the WebSocket API before ever asking for it,
            # so this only happens if the cache was evicted underneath a stale page.
            raise web.HTTPNotFound(text="clip not captured (or no longer cached)")
        return web.FileResponse(clip.path, headers={"Content-Type": "video/mp4"})


def async_register_views(hass: HomeAssistant) -> None:
    """Register the streaming views once."""
    if hass.data.get(_VIEWS_REGISTERED):
        return
    hass.http.register_view(KasaStreamView(hass))
    hass.http.register_view(KasaPlaybackView(hass))
    hass.http.register_view(KasaClipView(hass))
    hass.data[_VIEWS_REGISTERED] = True


_VIEWS_REGISTERED = f"{__name__}_views"
