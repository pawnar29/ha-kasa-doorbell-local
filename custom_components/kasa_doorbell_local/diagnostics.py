"""Diagnostics support for Kasa Doorbell (local) (redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import KasaDoorbellConfigEntry
from .const import CONF_EMAIL, CONF_PASSWORD, CONF_STREAM_PASSWORD

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_STREAM_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KasaDoorbellConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "thing_name": coordinator.thing_name,
        "has_stream_password": bool(coordinator.stream_password_b64),
    }
