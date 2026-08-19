"""Camera entity — fully self-contained local video.

The camera's transport (HTTPS on a nonstandard port, self-signed cert, legacy TLS ciphers,
Basic auth, chunked `multipart/x-mixed-replace` of H.264 + G.711) is something HA's stream
component cannot open, and HA's BUNDLED go2rtc cannot either — it ships without the `kasa`
driver compiled in (its /api/schemes lists no `kasa`).

So the integration converts the stream itself: `stream.py` reads the multipart feed and pipes
the H.264 through ffmpeg (always present in HA) as MPEG-TS, served by `view.py`. This entity
just points HA at that local, signed URL.

Result: no go2rtc, no Frigate, no downloads, no user configuration.
"""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KasaDoorbellConfigEntry
from .const import CONF_HOST
from .view import async_live_url

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: KasaDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the camera."""
    async_add_entities([KasaCamera(entry)])


class KasaCamera(Camera):
    """Live view, converted locally to something HA can play."""

    _attr_has_entity_name = True
    _attr_name = None  # use the device name
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: KasaDoorbellConfigEntry) -> None:
        super().__init__()
        self._entry = entry
        self._host: str = entry.data[CONF_HOST]
        self._attr_unique_id = f"{entry.entry_id}_camera"
        self._attr_device_info = entry.runtime_data.device_info

    async def stream_source(self) -> str | None:
        """A local, signed MPEG-TS URL that HA's stream component can open."""
        if not self._entry.runtime_data.stream_password_b64:
            _LOGGER.warning("No device secret available; camera cannot start")
            return None

        # Consumed by HA itself, so always go over loopback — avoids external URL config,
        # reverse proxies and DNS entirely. ffmpeg/PyAV don't verify TLS on input by default.
        return async_live_url(self.hass, self._entry.entry_id)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Let HA derive the thumbnail from the stream.

        The camera exposes no snapshot endpoint of its own, and pulling a second video
        connection just for a still would fight the device's single stream slot.
        """
        return None
