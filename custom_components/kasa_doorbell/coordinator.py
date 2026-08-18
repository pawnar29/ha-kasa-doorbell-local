"""Coordinator — fully local, model-agnostic.

Two LAN pollers, no cloud:
  * motion  — ``vod.get_detect_event_state`` (``is_moving``), instant (~1-2 s).
  * sysinfo — ``system.get_sysinfo``, slow (60 s). Carries identity (model/alias/MAC/firmware),
              capabilities (``c_opt`` cipher list, ``new_feature`` flags) and live state
              (LED, privacy switch, DND, RSSI) that drives the switch/sensor entities.

Capabilities are probed once at setup so optional entities only appear on models that have
them — Linkie answers an unknown module/method with a clean device error, making probing free.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .api import (
    KasaCameraInfo,
    LinkieError,
    LocalLinkieClient,
    async_probe_stream_port,
    build_linkie_ssl_context,
    secure_password,
)
from .const import (
    CONF_CIPHER,
    CONF_EMAIL,
    CONF_HOST,
    CONF_LOCAL_MOTION_INTERVAL,
    CONF_PASSWORD,
    CONF_STREAM_PASSWORD,
    CONF_THING_NAME,
    DEFAULT_CIPHER,
    DEFAULT_LOCAL_MOTION_INTERVAL,
    DEFAULT_SYSINFO_INTERVAL,
    DOMAIN,
    LINKIE_BATTERY_MODULE,
    LINKIE_MOTION_METHOD,
    LINKIE_PROBE_METHODS,
    LINKIE_VOD_MODULE,
    LOCAL_MOTION_FAILURE_THRESHOLD,
    MAX_LOCAL_MOTION_INTERVAL,
    MIN_LOCAL_MOTION_INTERVAL,
    STREAM_PORT,
    STREAM_PORT_CANDIDATES,
)

_LOGGER = logging.getLogger(__name__)

SIGNAL_LOCAL_MOTION = f"{DOMAIN}_local_motion"  # bool, or None when unavailable
SIGNAL_SYSINFO = f"{DOMAIN}_sysinfo"  # KasaCameraInfo

# Battery-powered models sleep; a 2 s poll would drain them and mostly fail. Back the motion
# poll right off when the battery module is present.
BATTERY_MOTION_INTERVAL = 30


class KasaDoorbellCoordinator:
    """Owns the LAN pollers and the camera's identity + capabilities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.host: str = entry.data[CONF_HOST]
        self.thing_name: str = entry.data.get(CONF_THING_NAME, "")
        # Local Basic-auth secret for video (:19443) and control (:10443). RE-DERIVED each
        # start from the account password using the cipher the config flow negotiated, so it
        # self-heals when the user changes their TP-Link password. An explicit secret stored
        # on the entry overrides (used when the user pasted one).
        self.stream_password_b64: str = (
            entry.data.get(CONF_STREAM_PASSWORD)
            or secure_password(
                entry.data[CONF_PASSWORD], entry.data.get(CONF_CIPHER, DEFAULT_CIPHER)
            )
        )
        # Stream port varies across the family (the app picks among 19443/18443/17443).
        self.stream_port: int = STREAM_PORT
        self.info: KasaCameraInfo | None = None
        self.capabilities: set[str] = set()  # module names confirmed present
        self.ssl_context = None  # relaxed TLS ctx, shared with the video pipeline
        self._local: LocalLinkieClient | None = None
        self._unsub_motion = None
        self._unsub_sysinfo = None
        self._local_fails = 0
        self._local_available = False

    # ── configuration ────────────────────────────────────────────────────────
    @property
    def motion_interval(self) -> int:
        """Motion poll cadence (seconds); slowed automatically on battery models."""
        raw = self.entry.options.get(
            CONF_LOCAL_MOTION_INTERVAL, DEFAULT_LOCAL_MOTION_INTERVAL
        )
        try:
            value = max(MIN_LOCAL_MOTION_INTERVAL, min(MAX_LOCAL_MOTION_INTERVAL, int(raw)))
        except (TypeError, ValueError):
            value = DEFAULT_LOCAL_MOTION_INTERVAL
        if LINKIE_BATTERY_MODULE in self.capabilities:
            return max(value, BATTERY_MOTION_INTERVAL)
        return value

    @property
    def client(self) -> LocalLinkieClient | None:
        """The shared local client, for entities that need to issue commands."""
        return self._local

    @property
    def device_info(self) -> DeviceInfo:
        """Model-agnostic HA device entry, populated from the camera's own sysinfo."""
        info = self.info
        device = DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            manufacturer="TP-Link",
            name=(info.alias or info.dev_name) if info else "Kasa Camera",
            model=(info.model or "Kasa Camera") if info else "Kasa Camera",
            sw_version=info.sw_ver if info else None,
            hw_version=info.hw_ver if info else None,
        )
        if info and info.mac:
            device["connections"] = {(CONNECTION_NETWORK_MAC, info.mac)}
        return device

    def supports(self, module: str) -> bool:
        """True if this model has the given Linkie module (probed at setup)."""
        return module in self.capabilities

    # ── setup / teardown ─────────────────────────────────────────────────────
    async def async_setup(self) -> None:
        """Identify the camera, probe its capabilities, then start the local pollers."""
        if not self.stream_password_b64:
            _LOGGER.warning("No device secret available; local polling disabled")
            return
        ssl_ctx = await self.hass.async_add_executor_job(build_linkie_ssl_context)
        self.ssl_context = ssl_ctx
        self._local = LocalLinkieClient(
            self.host,
            self.entry.data[CONF_EMAIL],
            self.stream_password_b64,
            async_get_clientsession(self.hass, verify_ssl=False),
            ssl_ctx,
        )

        # Identity — also confirms the secret/cipher actually works.
        try:
            self.info = await self._local.async_get_sysinfo()
            _LOGGER.debug(
                "Identified %s (%s) fw %s",
                self.info.model, self.info.alias, self.info.sw_ver,
            )
        except LinkieError as err:
            _LOGGER.warning("Could not read sysinfo from %s: %s", self.host, err)

        await self._async_probe_capabilities()

        # Which stream port this model listens on (19443 on the KD110).
        self.stream_port = await async_probe_stream_port(self.host, STREAM_PORT_CANDIDATES)
        if self.stream_port != STREAM_PORT:
            _LOGGER.debug("Using stream port %s", self.stream_port)

        self._unsub_motion = async_track_time_interval(
            self.hass, self._async_motion_poll, timedelta(seconds=self.motion_interval)
        )
        self._unsub_sysinfo = async_track_time_interval(
            self.hass, self._async_sysinfo_poll,
            timedelta(seconds=DEFAULT_SYSINFO_INTERVAL),
        )
        _LOGGER.debug(
            "Local pollers started (motion %ss, sysinfo %ss); capabilities: %s",
            self.motion_interval, DEFAULT_SYSINFO_INTERVAL,
            sorted(m.rsplit(".", 1)[-1] for m in self.capabilities) or "none",
        )

    async def _async_probe_capabilities(self) -> None:
        """Probe each optional module once so entities are created only where supported."""
        if self._local is None:
            return
        # Motion is the one non-optional capability; probe it the same way.
        probes = {LINKIE_VOD_MODULE: LINKIE_MOTION_METHOD, **LINKIE_PROBE_METHODS}
        for module, method in probes.items():
            try:
                if await self._local.async_supports(module, method):
                    self.capabilities.add(module)
            except LinkieError as err:  # auth/transport — abandon probing, keep what we have
                _LOGGER.debug("Capability probe stopped at %s: %s", module, err)
                return

    async def async_shutdown(self) -> None:
        """Stop polling."""
        for attr in ("_unsub_motion", "_unsub_sysinfo"):
            cancel = getattr(self, attr)
            if cancel:
                cancel()
                setattr(self, attr, None)

    # ── pollers ──────────────────────────────────────────────────────────────
    async def _async_motion_poll(self, _now) -> None:
        """Query the instant LAN motion state and broadcast it."""
        if self._local is None:
            return
        try:
            result = await self._local.async_get_motion_state()
        except LinkieError as err:
            self._local_fails += 1
            _LOGGER.debug("Local motion poll failed (%s): %s", self._local_fails, err)
            if self._local_available and self._local_fails >= LOCAL_MOTION_FAILURE_THRESHOLD:
                self._local_available = False
                _LOGGER.warning(
                    "Local motion unavailable — camera off-LAN, or the account password "
                    "changed (the local secret is derived from it)"
                )
                async_dispatcher_send(self.hass, SIGNAL_LOCAL_MOTION, None)
            return
        self._local_fails = 0
        if not self._local_available:
            self._local_available = True
            _LOGGER.info("Local real-time motion active (LAN)")
        async_dispatcher_send(
            self.hass, SIGNAL_LOCAL_MOTION, bool(result.get("is_moving"))
        )

    async def _async_sysinfo_poll(self, _now) -> None:
        """Refresh identity + live switch/RSSI state for the switch and sensor entities."""
        if self._local is None:
            return
        try:
            self.info = await self._local.async_get_sysinfo()
        except LinkieError as err:
            _LOGGER.debug("sysinfo poll failed: %s", err)
            return
        async_dispatcher_send(self.hass, SIGNAL_SYSINFO, self.info)

    async def async_refresh_sysinfo(self) -> None:
        """Force a sysinfo refresh — used right after a switch write."""
        await self._async_sysinfo_poll(None)

    # ── recordings (SD card) ─────────────────────────────────────────────────
    @property
    def has_recordings(self) -> bool:
        """True if this model has the VOD module, i.e. can serve recorded footage."""
        return LINKIE_VOD_MODULE in self.capabilities

    async def async_get_record_range(self) -> tuple[int, int] | None:
        """Sliding window of footage on the SD card, or None (no card / not supported)."""
        if self._local is None or not self.has_recordings:
            return None
        try:
            return await self._local.async_get_record_range()
        except LinkieError as err:
            _LOGGER.debug("Record range read failed: %s", err)
            return None

    async def async_get_detections(self, start_ts: int, end_ts: int) -> list[dict]:
        """Finalized detection clips in a window: {start_time, end_time, alarm_type}."""
        if self._local is None or not self.has_recordings:
            return []
        try:
            return await self._local.async_get_detections(start_ts, end_ts)
        except LinkieError as err:
            _LOGGER.debug("Detection list read failed: %s", err)
            return []

    async def async_get_battery(self) -> dict | None:
        """Battery status on models that have one (KC400/KC401/EC70). None otherwise."""
        if self._local is None or LINKIE_BATTERY_MODULE not in self.capabilities:
            return None
        try:
            return await self._local.async_get(LINKIE_BATTERY_MODULE, "get_status")
        except LinkieError as err:
            _LOGGER.debug("Battery read failed: %s", err)
            return None

    @callback
    def _ingest(self, event) -> None:  # pragma: no cover - kept for optional event sources
        """Dispatch an event to entities."""
        _LOGGER.debug("KD110 event: %s", getattr(event, "event_name", event))
