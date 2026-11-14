"""Serve the recordings Lovelace card and load it automatically.

Registered as an extra frontend module so the user never has to add a Lovelace *resource*
by hand — the card type is simply available once the integration is set up.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "kasa-recordings-card.js"
CARD_URL = f"/kasa_doorbell_local_static/{CARD_FILENAME}"
_REGISTERED = f"{__name__}_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Publish the card JS and add it to the frontend's module list (once)."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True

    path = Path(__file__).parent / "www" / CARD_FILENAME
    if not path.is_file():  # pragma: no cover - only if the install is incomplete
        _LOGGER.warning("Recordings card not found at %s; card unavailable", path)
        return

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(path), False)]
        )
    except ImportError:  # pragma: no cover - cores before StaticPathConfig existed
        hass.http.register_static_path(CARD_URL, str(path), False)

    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.loader import async_get_integration

    from .const import DOMAIN

    # Cache-bust on upgrade: browsers hold frontend modules aggressively, and a stale card
    # talking to a new WebSocket API is a confusing failure to debug.
    try:
        version = str((await async_get_integration(hass, DOMAIN)).version or "")
    except Exception:  # noqa: BLE001 — versioning is a nicety, not a requirement
        version = ""

    add_extra_js_url(hass, f"{CARD_URL}?v={version}" if version else CARD_URL)
    _LOGGER.debug("Recordings card registered at %s (v%s)", CARD_URL, version or "?")
