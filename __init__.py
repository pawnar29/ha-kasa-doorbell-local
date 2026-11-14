"""The Kasa Doorbell (local) integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import websocket
from .clips import async_get_cache
from .coordinator import KasaDoorbellCoordinator
from .frontend import async_register_card
from .view import async_register_views, async_stop_pipeline

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CAMERA,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SENSOR,
]

type KasaDoorbellConfigEntry = ConfigEntry[KasaDoorbellCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: KasaDoorbellConfigEntry
) -> bool:
    """Set up a Kasa camera from a config entry."""
    async_register_views(hass)
    websocket.async_register(hass)
    await async_register_card(hass)
    coordinator = KasaDoorbellCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: KasaDoorbellConfigEntry
) -> None:
    """Reload the entry when options (e.g. scan interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: KasaDoorbellConfigEntry
) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await async_stop_pipeline(hass, entry.entry_id)
        async_get_cache(hass).shutdown()
        await entry.runtime_data.async_shutdown()
    return unloaded
