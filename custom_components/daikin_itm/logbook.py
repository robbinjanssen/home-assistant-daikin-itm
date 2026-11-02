"""Describe the decisions of auto mode in the logbook."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_FRIENDLY_NAME
from homeassistant.core import callback
from homeassistant.helpers.translation import async_get_cached_translations

from .const import DOMAIN, EVENT_AUTO_MODE

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import Event, HomeAssistant

# Translations of the auto mode sensor name the modes, reasons and attributes.
SENSOR = f"component.{DOMAIN}.entity.sensor.auto_mode"


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe the auto mode event.

    Args:
    ----
        hass: The HomeAssistant instance.
        async_describe_event: Registers a description.

    """

    @callback
    def describe(event: Event) -> dict[str, str]:
        data = event.data
        entity_id = data[ATTR_ENTITY_ID]
        texts = async_get_cached_translations(
            hass, hass.config.language, "entity", DOMAIN
        )

        def text(key: str, default: str) -> str:
            return texts.get(f"{SENSOR}.{key}", default)

        mode = text(f"state.{data['mode']}", str(data["mode"]))
        reason = text(
            f"state_attributes.reason.state.{data['reason']}", str(data["reason"])
        )
        details = [
            f"{text(f'state_attributes.{attr}.name', attr)} {data[attr]} °C"
            for attr in ("temperature", "target")
            if data.get(attr) is not None
        ]
        if data.get("outdoor") is not None:
            name = text("state_attributes.outdoor_temperature.name", "outdoor")
            details.append(f"{name} {data['outdoor']} °C")
        state = hass.states.get(entity_id)
        return {
            LOGBOOK_ENTRY_NAME: (
                state.attributes.get(ATTR_FRIENDLY_NAME, entity_id)
                if state
                else entity_id
            ),
            LOGBOOK_ENTRY_MESSAGE: f"{mode}: {reason} ({', '.join(details)})",
            LOGBOOK_ENTRY_ENTITY_ID: entity_id,
        }

    async_describe_event(DOMAIN, EVENT_AUTO_MODE, describe)
