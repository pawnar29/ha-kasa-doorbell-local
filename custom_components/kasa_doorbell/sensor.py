"""Diagnostic sensors, read from the coordinator's existing ``system.get_sysinfo`` poll.

No extra device traffic — these just surface fields the sysinfo refresh already returns.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KasaDoorbellConfigEntry
from .api import KasaCameraInfo
from .const import LINKIE_BATTERY_MODULE
from .coordinator import SIGNAL_SYSINFO


@dataclass(frozen=True, kw_only=True)
class KasaSensorDescription:
    """Describes one value pulled out of sysinfo."""

    key: str
    name: str
    value: Callable[[KasaCameraInfo], int | str | None]
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    unit: str | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC


SENSORS: tuple[KasaSensorDescription, ...] = (
    KasaSensorDescription(
        key="rssi",
        name="Wi-Fi signal",
        value=lambda info: info.rssi,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        unit=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    ),
    KasaSensorDescription(
        key="resolution",
        name="Resolution",
        value=lambda info: info.resolution or None,
        icon="mdi:video-high-definition",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KasaDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create diagnostic sensors for whatever sysinfo actually reported."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    if (info := coordinator.info) is not None:
        entities += [
            KasaCameraSensor(entry, desc) for desc in SENSORS if desc.value(info) is not None
        ]
    # Battery models only (KC400/KC401/EC70) — the module is absent on mains-powered cams.
    if coordinator.supports(LINKIE_BATTERY_MODULE):
        entities.append(KasaBatterySensor(entry))
    async_add_entities(entities)


class KasaBatterySensor(SensorEntity):
    """Battery level, polled directly from the battery module on models that have one."""

    _attr_has_entity_name = True
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: KasaDoorbellConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_battery"
        self._attr_device_info = entry.runtime_data.device_info

    async def async_update(self) -> None:
        """Polled by HA (battery changes slowly, so the default interval is fine)."""
        status = await self._entry.runtime_data.async_get_battery()
        if not status:
            return
        percent = status.get("battery_percent")
        if isinstance(percent, int):
            self._attr_native_value = percent
        charging = status.get("battery_charging")
        self._attr_extra_state_attributes = {
            "charging": charging,
            "capacity": status.get("battery_capacity"),
            "temperature": status.get("battery_temperature"),
            "installed": status.get("battery_installed"),
            "model": status.get("battery_model"),
        }


class KasaCameraSensor(SensorEntity):
    """One diagnostic value from sysinfo."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: KasaDoorbellConfigEntry, description: KasaSensorDescription
    ) -> None:
        self._entry = entry
        self._desc = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_native_unit_of_measurement = description.unit
        self._attr_entity_category = description.entity_category
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = entry.runtime_data.device_info
        info = entry.runtime_data.info
        self._attr_native_value = description.value(info) if info else None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SYSINFO, self._handle_sysinfo)
        )

    @callback
    def _handle_sysinfo(self, info: KasaCameraInfo) -> None:
        value = self._desc.value(info)
        if value != self._attr_native_value:
            self._attr_native_value = value
            self.async_write_ha_state()
