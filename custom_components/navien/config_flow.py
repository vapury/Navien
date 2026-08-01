"""Config flow for Navien Smart."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import NavienSmartApiClient, NavienSmartApiError, NavienSmartAuthError
from .const import DOMAIN


class NavienSmartConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Navien Smart."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(username)
            self._abort_if_unique_id_configured()

            client = NavienSmartApiClient(
                session=async_get_clientsession(self.hass),
                username=username,
                password=password,
            )

            try:
                await client.async_login()
            except NavienSmartAuthError:
                errors["base"] = "invalid_auth"
            except NavienSmartApiError:
                errors["base"] = "cannot_connect"
            else:
                pending = self.hass.data.setdefault(DOMAIN, {}).setdefault("_pending_auth", {})
                pending[username] = client.export_auth_state()
                return self.async_create_entry(
                    title=username,
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return NavienSmartOptionsFlow()


class NavienSmartOptionsFlow(config_entries.OptionsFlow):
    """Handle Navien Smart options."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Manage options."""
        return self.async_create_entry(title="", data={})
