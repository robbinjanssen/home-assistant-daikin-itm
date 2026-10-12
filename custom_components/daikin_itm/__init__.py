"""Daikin intelligent Touch Manager (iTM) integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .api import base_url, build_client
from .const import DOMAIN
from .coordinator import HassSessionStore, ItmConfigEntry, ItmCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CLIMATE, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ItmConfigEntry) -> bool:
    """Set up the iTM from a config entry.

    Args:
    ----
        hass: The HomeAssistant instance.
        entry: The config entry.

    Returns:
    -------
        True once the platforms are set up.

    """
    store = HassSessionStore(hass, entry.data[CONF_HOST])
    entry.async_on_unload(store.async_cancel_write)
    client = build_client(hass, entry.data, store)
    coordinator = ItmCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    # The iTM itself; the indoor units are added as its children.
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Daikin",
        model="intelligent Touch Manager",
        name=entry.title,
        sw_version=client.web_version,
        configuration_url=base_url(entry.data[CONF_HOST]),
    )
    coordinator.hub_device_id = hub.id

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ItmConfigEntry) -> bool:
    """Unload a config entry.

    No session is held between polls, so there is nothing to log out here.

    Args:
    ----
        hass: The HomeAssistant instance.
        entry: The config entry.

    Returns:
    -------
        True if the platforms were unloaded.

    """
    # The count of refused logins starts again after loading.
    ir.async_delete_issue(hass, DOMAIN, f"login_rejected_{entry.entry_id}")
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ItmConfigEntry) -> None:
    """Remove the stored session token of a removed config entry.

    Args:
    ----
        hass: The HomeAssistant instance.
        entry: The config entry.

    """
    await HassSessionStore(hass, entry.data[CONF_HOST]).async_remove()


async def async_remove_config_entry_device(
    _hass: HomeAssistant, entry: ItmConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing the device of a unit that is no longer in the zone.

    Args:
    ----
        _hass: The HomeAssistant instance.
        entry: The config entry.
        device: The device to remove.

    Returns:
    -------
        True if the device may be removed.

    """
    units = {f"{entry.entry_id}_{key}" for key in entry.runtime_data.data or {}}
    return not any(
        domain == DOMAIN and (identifier == entry.entry_id or identifier in units)
        for domain, identifier in device.identifiers
    )
