"""Config flow: host, credentials and the zone that holds the indoor units.

The options set the location of the outdoor temperature auto mode uses.
"""

from __future__ import annotations

import contextlib
from typing import Any

import aiohttp
import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_LATITUDE,
    CONF_LOCATION,
    CONF_LONGITUDE,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    LocationSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from daikin_itm import ItmError, LoginRejected, ZoneNotFound

from .api import build_client, unique_id_for_host, zone_path
from .const import CONF_ZONE_PATH, DEFAULT_ZONE_PATH, DOMAIN, LOGGER
from .coordinator import HassSessionStore
from .weather import weather_location

SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): str,
        probatio.Required(CONF_USERNAME): str,
        probatio.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        probatio.Optional(CONF_ZONE_PATH, default=DEFAULT_ZONE_PATH): str,
    }
)


class ItmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up or change the connection to an iTM."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry) -> ItmOptionsFlow:
        """Return the options flow.

        Args:
        ----
            _config_entry: The config entry.

        Returns:
        -------
            The options flow.

        """
        return ItmOptionsFlow()

    async def _async_validate(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Log in and read the units of the zone.

        Args:
        ----
            user_input: The input received from the user.

        Returns:
        -------
            The errors by field, and the placeholders for their messages.

        """
        placeholders = {"available": ""}
        try:
            store = HassSessionStore(self.hass, user_input[CONF_HOST])
            client = build_client(self.hass, user_input, store)
            units = await client.fetch_units()
        except LoginRejected:
            return {"base": "login_rejected"}, placeholders
        except ZoneNotFound as err:
            placeholders["available"] = ", ".join(err.available)
            return {CONF_ZONE_PATH: "zone_not_found"}, placeholders
        except ItmError, aiohttp.ClientError, TimeoutError:
            return {"base": "cannot_connect"}, placeholders
        except Exception:  # noqa: BLE001 # pylint: disable=broad-exception-caught
            LOGGER.exception("Unexpected error validating the iTM")
            return {"base": "unknown"}, placeholders
        if not units:
            # Probably a level with zones only: name them, to help find the path.
            path = zone_path(user_input.get(CONF_ZONE_PATH, DEFAULT_ZONE_PATH))
            with contextlib.suppress(ItmError, aiohttp.ClientError, TimeoutError):
                placeholders["available"] = ", ".join(await client.list_zones(path))
            return {CONF_ZONE_PATH: "no_units"}, placeholders
        return {}, placeholders

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow started by the user.

        Args:
        ----
            user_input: The input received from the user or none.

        Returns:
        -------
            The created entry or a form with errors.

        """
        errors: dict[str, str] = {}
        placeholders = {"available": ""}
        if user_input is not None:
            await self.async_set_unique_id(unique_id_for_host(user_input[CONF_HOST]))
            self._abort_if_unique_id_configured()
            errors, placeholders = await self._async_validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"Daikin iTM ({user_input[CONF_HOST]})", data=user_input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the host, credentials or zone of an existing entry.

        Args:
        ----
            user_input: The input received from the user or none.

        Returns:
        -------
            An abort result after updating the entry or a form with errors.

        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders = {"available": ""}
        if user_input is not None:
            unique_id = unique_id_for_host(user_input[CONF_HOST])
            if unique_id != entry.unique_id:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
            errors, placeholders = await self._async_validate(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=unique_id,
                    title=f"Daikin iTM ({user_input[CONF_HOST]})",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                SCHEMA, user_input or dict(entry.data)
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


class ItmOptionsFlow(OptionsFlowWithReload):
    """Change the location of the outdoor temperature for auto mode."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show or save the options.

        Args:
        ----
            user_input: The input received from the user or none.

        Returns:
        -------
            The saved options or the form.

        """
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        latitude, longitude = weather_location(self.hass, self.config_entry)
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_LOCATION,
                    default={CONF_LATITUDE: latitude, CONF_LONGITUDE: longitude},
                ): LocationSelector()
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
