"""Diagnostics for the Daikin iTM integration."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import ItmConfigEntry

TO_REDACT = {CONF_HOST, CONF_PASSWORD, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: ItmConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Args:
    ----
        _hass: The HomeAssistant instance.
        entry: The config entry.

    Returns:
    -------
        The entry's settings without credentials, and what was read from the iTM.

    """
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "web_version": coordinator.client.web_version,
        "last_update_success": coordinator.last_update_success,
        "capabilities": {
            key: dataclasses.asdict(caps)
            for key, caps in coordinator.capabilities.items()
        },
        "units": {
            key: dataclasses.asdict(unit)
            for key, unit in (coordinator.data or {}).items()
        },
    }
