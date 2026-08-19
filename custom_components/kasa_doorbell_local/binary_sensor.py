"""Motion binary sensor — driven by the camera's local real-time motion state.

The LAN query (``get_detect_event_state``) returns ``is_moving``, a true bidirectional state,
so the sensor turns on and off directly from the device with ~1-2s latency and no cloud.

A reset timer is kept as a safety net only: if the local poll dies mid-detection (camera off
LAN, password changed), the sensor clears itself instead of latching on forever.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import KasaDoorbellConfigEntry
from .const import (
    CONF_MOTION_RESET,
    DEFAULT_MOTION_RESET_SECONDS,
    LINKIE_VOD_MODULE,
    MAX_MOTION_RESET_SECONDS,
    MIN_MOTION_RESET_SECONDS,
)
from .coordinator import SIGNAL_LOCAL_MOTION

_LOGGER = logging.getLogger(__name__)


def _motion_reset_seconds(entry: KasaDoorbellConfigEntry) -> float:
    """Resolve the motion safety-reset window from options, clamped to sane bounds."""
    raw = entry.options.get(CONF_MOTION_RESET, DEFAULT_MOTION_RESET_SECONDS)
    try:
        return max(MIN_MOTION_RESET_SECONDS, min(MAX_MOTION_RESET_SECONDS, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_MOTION_RESET_SECONDS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KasaDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the motion binary sensor, if this model reports motion state."""
    coordinator = entry.runtime_data
    # Probed at setup — absent on models without the VOD module. Fall back to creating it
    # when probing was inconclusive (no capabilities gathered at all), so a probe failure
    # never silently costs the user their motion sensor.
    if coordinator.capabilities and not coordinator.supports(LINKIE_VOD_MODULE):
        _LOGGER.debug("Model reports no VOD module; skipping the motion sensor")
        return
    async_add_entities([KasaMotionSensor(entry)])


class KasaMotionSensor(BinarySensorEntity):
    """Motion at the doorbell, from the camera's own local detection state."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, entry: KasaDoorbellConfigEntry) -> None:
        self._entry = entry
        self._reset_after = _motion_reset_seconds(entry)
        self._cancel_reset = None
        self._attr_unique_id = f"{entry.entry_id}_motion"
        self._attr_is_on = False
        self._attr_device_info = entry.runtime_data.device_info

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's local motion signal."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_LOCAL_MOTION, self._handle_local_motion
            )
        )
        self.async_on_remove(self._clear_timer)

    @callback
    def _handle_local_motion(self, state: bool | None) -> None:
        """Local reading: authoritative on/off. None = local motion unavailable."""
        if state is None:
            # Poller gave up — don't leave the sensor stuck on.
            self._clear_timer()
            self._attr_available = False
            self._set(False)
            self.async_write_ha_state()
            return
        self._attr_available = True
        self._set(bool(state))
        self._clear_timer()
        if state:
            # Safety net: clear if readings stop arriving mid-detection.
            self._cancel_reset = async_call_later(
                self.hass, self._reset_after, self._auto_reset
            )

    @callback
    def _auto_reset(self, _now) -> None:
        self._cancel_reset = None
        _LOGGER.debug("motion: no local reading for %ss, clearing", self._reset_after)
        self._set(False)

    @callback
    def _clear_timer(self) -> None:
        if self._cancel_reset:
            self._cancel_reset()
            self._cancel_reset = None

    @callback
    def _set(self, state: bool) -> None:
        if state != self._attr_is_on:
            self._attr_is_on = state
            self.async_write_ha_state()
