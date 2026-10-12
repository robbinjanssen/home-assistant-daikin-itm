"""Create the iTM client from config entry data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from daikin_itm import ItmClient

from .const import CONF_ZONE_PATH, DEFAULT_ZONE_PATH

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

    from daikin_itm import SessionStore


def base_url(host: str) -> str:
    """Return the URL of the iTM web UI for a host setting.

    Args:
    ----
        host: The host as entered: "host", "host:port" or a full URL.

    Returns:
    -------
        The URL, http:// unless the setting says otherwise.

    """
    host = host.strip().rstrip("/")
    return host if "://" in host else f"http://{host}"


def unique_id_for_host(host: str) -> str:
    """Return the config entry unique ID for a host setting.

    "10.0.0.5", "http://10.0.0.5/" and "https://10.0.0.5" are the same iTM.

    Args:
    ----
        host: The host as entered.

    Returns:
    -------
        The host without scheme and trailing slash, in lower case.

    """
    return base_url(host).split("://", 1)[1].lower()


def zone_path(value: str) -> tuple[str, ...]:
    """Split a zone path setting such as "Building/Indoor units" into zone names.

    Args:
    ----
        value: The zone path as entered.

    Returns:
    -------
        The zone names from the top level down.

    """
    return tuple(zone.strip() for zone in value.split("/") if zone.strip())


def build_client(
    hass: HomeAssistant, data: Mapping[str, Any], store: SessionStore | None = None
) -> ItmClient:
    """Create an iTM client from config entry data.

    Args:
    ----
        hass: The HomeAssistant instance.
        data: The config entry data or the config flow input.
        store: Where the client keeps the session token, in memory if not given.

    Returns:
    -------
        The client.

    """
    return ItmClient(
        base_url(data[CONF_HOST]),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        session=async_get_clientsession(hass),
        store=store,
        zone_path=zone_path(data.get(CONF_ZONE_PATH, DEFAULT_ZONE_PATH)),
    )
