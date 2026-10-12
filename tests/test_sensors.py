"""Tests for the sensors and binary sensors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from . import state_of

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .fake_itm import FakeITM


async def test_sensors(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test the room temperature and problem sensors."""
    itm.units[1].temp = 19.5
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = state_of(hass, "sensor.meeting_room_room_temperature")
    assert state.state == "19.5"
    assert state.attributes["state_class"] == "measurement"
    malfunction = state_of(hass, "binary_sensor.meeting_room_malfunction")
    assert malfunction.state == "off"
    assert malfunction.attributes["error_code"] is None
    assert state_of(hass, "binary_sensor.meeting_room_filter").state == "off"
    entity = er.async_get(hass).async_get("binary_sensor.meeting_room_filter")
    assert entity is not None
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
