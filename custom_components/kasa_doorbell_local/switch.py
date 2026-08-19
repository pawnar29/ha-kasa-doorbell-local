"""Switches for local camera settings, read from ``system.get_sysinfo`` and written over LAN.

All state comes from the single sysinfo poll the coordinator already runs, so these entities
cost no extra traffic. Each is created only if the model actually has the backing module
(probed once at setup), which is what makes this work across the Kasa camera family.

Field shapes differ per module and are taken from the decompiled app:
  * ``switch.set_is_enable`` and ``led.set_status`` take a **string** ``{"value": "on"|"off"}``
  * ``dndSchedule.set_dnd_enable`` takes a **boolean** ``{"enable": true|false}``
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KasaDoorbellConfigEntry
from .api import KasaCameraInfo, LinkieError
from .const import (
    LINKIE_DND_MODULE,
    LINKIE_LED_MODULE,
    LINKIE_SWITCH_MODULE,
)
from .coordinator import SIGNAL_SYSINFO

_LOGGER = logging.getLogger(__name__)


def _on_off(value: bool) -> dict[str, str]:
    return {"value": "on" if value else "off"}


def _enable(value: bool) -> dict[str, bool]:
    return {"enable": value}


@dataclass(frozen=True, kw_only=True)
class KasaSwitchDescription:
    """Describes one local camera setting exposed as a switch."""

    key: str
    name: str
    module: str
    set_method: str
    payload: Callable[[bool], dict[str, Any]]
    value: Callable[[KasaCameraInfo], bool | None]
    icon: str | None = None
    entity_category: EntityCategory | None = None


SWITCHES: tuple[KasaSwitchDescription, ...] = (
    KasaSwitchDescription(
        key="camera_active",
        name="Camera active",  # off == the camera's privacy mode
        module=LINKIE_SWITCH_MODULE,
        set_method="set_is_enable",
        payload=_on_off,
        value=lambda info: info.camera_on,
        icon="mdi:cctv",
    ),
    KasaSwitchDescription(
        key="status_led",
        name="Status LED",
        module=LINKIE_LED_MODULE,
        set_method="set_status",
        payload=_on_off,
        value=lambda info: info.led_on,
        icon="mdi:led-on",
        entity_category=EntityCategory.CONFIG,
    ),
    KasaSwitchDescription(
        key="do_not_disturb",
        name="Do not disturb",
        module=LINKIE_DND_MODULE,
        set_method="set_dnd_enable",
        payload=_enable,
        value=lambda info: info.dnd_on,
        icon="mdi:bell-off",
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KasaDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a switch for each setting this model supports."""
    coordinator = entry.runtime_data
    entities = [
        KasaCameraSwitch(entry, desc)
        for desc in SWITCHES
        # When probing produced nothing at all (e.g. it was cut short), fall back to the
        # sysinfo field being present rather than creating no switches.
        if coordinator.supports(desc.module)
        or (not coordinator.capabilities and coordinator.info is not None
            and desc.value(coordinator.info) is not None)
    ]
    async_add_entities(entities)


class KasaCameraSwitch(SwitchEntity):
    """One local camera setting."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: KasaDoorbellConfigEntry, description: KasaSwitchDescription
    ) -> None:
        self._entry = entry
        self._desc = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_entity_category = description.entity_category
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = entry.runtime_data.device_info
        info = entry.runtime_data.info
        self._attr_is_on = description.value(info) if info else None

    async def async_added_to_hass(self) -> None:
        """Track the coordinator's sysinfo refreshes."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SYSINFO, self._handle_sysinfo)
        )

    @callback
    def _handle_sysinfo(self, info: KasaCameraInfo) -> None:
        value = self._desc.value(info)
        if value != self._attr_is_on:
            self._attr_is_on = value
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, value: bool) -> None:
        """Write the setting, then re-read sysinfo so state reflects the device."""
        coordinator = self._entry.runtime_data
        client = coordinator.client
        if client is None:
            return
        try:
            await client.async_set(
                self._desc.module, self._desc.set_method, self._desc.payload(value)
            )
        except LinkieError as err:
            _LOGGER.error("Failed to set %s: %s", self._desc.name, err)
            return
        # Optimistic, then confirmed by the refresh below.
        self._attr_is_on = value
        self.async_write_ha_state()
        await coordinator.async_refresh_sysinfo()
