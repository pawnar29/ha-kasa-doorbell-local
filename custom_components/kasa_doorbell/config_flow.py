"""Config flow — fully local. No cloud login, no cloud device list.

The camera tells us everything setup needs over the LAN: ``system.get_sysinfo`` returns the
deviceId, model, alias, MAC and firmware, plus ``c_opt`` (which cipher to secure the password
with). So the user supplies only host + account email/password, and we identify the device
ourselves — the TP-Link cloud is never contacted.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    KasaCameraInfo,
    LinkieAuthError,
    LinkieError,
    LocalLinkieClient,
    build_linkie_ssl_context,
    secure_password,
)
from .const import (
    CONF_CIPHER,
    CONF_EMAIL,
    CONF_HOST,
    CONF_LOCAL_MOTION_INTERVAL,
    CONF_MOTION_RESET,
    CONF_PASSWORD,
    CONF_STREAM_PASSWORD,
    CONF_THING_NAME,
    DEFAULT_CIPHER,
    DEFAULT_LOCAL_MOTION_INTERVAL,
    DEFAULT_MOTION_RESET_SECONDS,
    DOMAIN,
    MAX_LOCAL_MOTION_INTERVAL,
    MAX_MOTION_RESET_SECONDS,
    MIN_LOCAL_MOTION_INTERVAL,
    MIN_MOTION_RESET_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_STREAM_PASSWORD): str,  # optional explicit secret override
    }
)


class KasaDoorbellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Identify the camera over the LAN and create the entry. No cloud."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info, cipher = await self._async_identify(user_input)
            except LinkieAuthError:
                errors["base"] = "invalid_auth"
            except LinkieError as err:
                _LOGGER.warning("Could not reach the camera at %s: %s",
                                user_input[CONF_HOST], err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info.device_id or user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                data = {
                    **user_input,
                    CONF_THING_NAME: info.device_id,
                    # Persist WHICH cipher worked, not the derived secret — so the secret is
                    # re-derived each start and self-heals when the password changes.
                    CONF_CIPHER: cipher,
                }
                if not (user_input.get(CONF_STREAM_PASSWORD) or "").strip():
                    data.pop(CONF_STREAM_PASSWORD, None)
                name = info.alias or info.dev_name or info.model_family or "Kasa Camera"
                return self.async_create_entry(
                    title=f"{name} ({info.model_family or user_input[CONF_HOST]})",
                    data=data,
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def _async_identify(
        self, user_input: dict[str, Any]
    ) -> tuple[KasaCameraInfo, str]:
        """Return (device info, cipher name), negotiating the cipher against the device.

        The KD110 authenticates local access with md5(account password); older models may
        only accept base64(password). Try md5 first, then base64. An explicit secret
        override short-circuits the negotiation.
        """
        host = user_input[CONF_HOST]
        email = user_input[CONF_EMAIL]
        password = user_input[CONF_PASSWORD]
        override = (user_input.get(CONF_STREAM_PASSWORD) or "").strip()

        ssl_ctx = await self.hass.async_add_executor_job(build_linkie_ssl_context)
        session = async_get_clientsession(self.hass, verify_ssl=False)

        if override:
            attempts: list[tuple[str, str]] = [(override, DEFAULT_CIPHER)]
        else:
            attempts = [
                (secure_password(password, name), name) for name in ("md5", "base64")
            ]

        last_auth_error: LinkieAuthError | None = None
        for secret, cipher in attempts:
            client = LocalLinkieClient(host, email, secret, session, ssl_ctx)
            try:
                info = await client.async_get_sysinfo()
            except LinkieAuthError as err:
                last_auth_error = err
                continue
            return info, cipher
        raise last_auth_error or LinkieAuthError("no cipher accepted")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return KasaDoorbellOptionsFlow()


class KasaDoorbellOptionsFlow(OptionsFlow):
    """Options: motion poll cadence and the motion safety-clear delay."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        current_reset = opts.get(CONF_MOTION_RESET, DEFAULT_MOTION_RESET_SECONDS)
        current_local = opts.get(CONF_LOCAL_MOTION_INTERVAL, DEFAULT_LOCAL_MOTION_INTERVAL)
        schema = vol.Schema(
            {
                vol.Optional(CONF_LOCAL_MOTION_INTERVAL, default=current_local): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_LOCAL_MOTION_INTERVAL, max=MAX_LOCAL_MOTION_INTERVAL),
                ),
                vol.Optional(CONF_MOTION_RESET, default=current_reset): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_MOTION_RESET_SECONDS, max=MAX_MOTION_RESET_SECONDS),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
