"""Tests for the climate entities."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import aiohttp
import pytest
from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    HVACMode,
)
from homeassistant.components.climate import DOMAIN as CLIMATE
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from . import state_of

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .fake_itm import FakeITM

ROOM = "climate.meeting_room"  # 1:1-04, running, cooling
STOPPED = "climate.1_1_09"  # 1:1-09, stopped, no description


async def _call(
    hass: HomeAssistant, service: str, entity_id: str, **data: object
) -> None:
    await hass.services.async_call(
        CLIMATE, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


@pytest.mark.usefixtures("init_integration")
async def test_climate_entities(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test the state of the units."""
    state = state_of(hass, ROOM)
    assert state.state == HVACMode.COOL
    assert state.attributes["temperature"] == 20
    assert state.attributes["current_temperature"] == 21.0
    assert (state.attributes["min_temp"], state.attributes["max_temp"]) == (20, 25)
    assert state.attributes["itm_name"] == "1:1-04"
    assert "error_code" not in state.attributes
    stopped = state_of(hass, STOPPED)
    assert stopped.state == HVACMode.OFF
    assert stopped.attributes["temperature"] is None  # not settable while stopped
    assert itm.session is None


@pytest.mark.usefixtures("init_integration")
async def test_modes_and_fan_from_capabilities(hass: HomeAssistant) -> None:
    """Test the modes and fan speeds are the ones the unit offers."""
    attributes = state_of(hass, ROOM).attributes
    assert attributes["hvac_modes"] == [
        HVACMode.OFF,
        HVACMode.FAN_ONLY,
        HVACMode.COOL,
        HVACMode.HEAT,
    ]
    assert attributes["fan_modes"] == ["low", "medium", "high", "auto"]
    assert attributes["fan_mode"] == "medium"
    attributes = state_of(hass, STOPPED).attributes
    assert HVACMode.DRY in attributes["hvac_modes"]
    assert HVACMode.HEAT_COOL in attributes["hvac_modes"]
    assert "fan_modes" not in attributes  # no fan speed control on this unit


async def test_unknown_mode_label_is_not_reported_as_heat_cool(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test a mode label the integration does not know (e.g. another language)."""
    itm.units[1].modes = ("Ventilator", "Koelen", "Verwarmen", "Iets anders")
    itm.units[1].mode = "Iets anders"
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    state = state_of(hass, ROOM)
    assert state.state == "unknown"
    assert HVACMode.HEAT_COOL not in state.attributes["hvac_modes"]


@pytest.mark.usefixtures("init_integration")
async def test_set_temperature(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test changing the setpoint."""
    await _call(hass, SERVICE_SET_TEMPERATURE, ROOM, **{ATTR_TEMPERATURE: 22})
    assert itm.units[1].setpoint == 22
    assert state_of(hass, ROOM).attributes["temperature"] == 22
    assert itm.session is None


@pytest.mark.usefixtures("init_integration")
async def test_set_temperature_stopped_unit(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test the setpoint of a stopped unit is refused before reaching the iTM."""
    requests = len(itm.requests)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_SET_TEMPERATURE, STOPPED, **{ATTR_TEMPERATURE: 20})
    assert err.value.translation_key == "unit_stopped"
    assert len(itm.requests) == requests


@pytest.mark.usefixtures("init_integration")
async def test_set_temperature_and_turn_off_sets_temperature_first(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test the setpoint is changed while the unit still runs."""
    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        ROOM,
        **{ATTR_TEMPERATURE: 24, ATTR_HVAC_MODE: HVACMode.OFF},
    )
    assert itm.units[1].setpoint == 24
    assert itm.units[1].running is False
    assert state_of(hass, ROOM).state == HVACMode.OFF


@pytest.mark.usefixtures("init_integration")
async def test_set_temperature_with_mode_starts_a_stopped_unit(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test a stopped unit is started before its setpoint changes."""
    itm.latency_logins = 2  # the start reaches the unit only after a while
    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        STOPPED,
        **{ATTR_TEMPERATURE: 18, ATTR_HVAC_MODE: HVACMode.HEAT},
    )
    itm.latency_logins = 0
    await _call(hass, SERVICE_SET_FAN_MODE, ROOM, **{ATTR_FAN_MODE: "low"})
    unit = itm.units[2]
    assert (unit.running, unit.mode) == (True, "Verwarmen")
    assert itm.overlaps == 0
    state = state_of(hass, STOPPED)
    assert state.state == HVACMode.HEAT
    assert state.attributes["temperature"] == 18


@pytest.mark.usefixtures("init_integration")
async def test_set_hvac_mode(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test changing the operation mode."""
    await _call(hass, SERVICE_SET_HVAC_MODE, ROOM, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    assert itm.units[1].mode == "Verwarmen"
    state = state_of(hass, ROOM)
    assert state.state == HVACMode.HEAT
    assert state.attributes["itm_mode"] == "Verwarmen"


@pytest.mark.usefixtures("init_integration")
async def test_set_hvac_mode_starts_stopped_unit(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test choosing a mode starts a stopped unit in one write."""
    await _call(hass, SERVICE_SET_HVAC_MODE, STOPPED, **{ATTR_HVAC_MODE: HVACMode.HEAT})
    assert itm.units[2].running
    assert itm.units[2].mode == "Verwarmen"
    assert state_of(hass, STOPPED).state == HVACMode.HEAT


@pytest.mark.usefixtures("init_integration")
async def test_set_hvac_mode_starts_a_unit_stopped_since_the_last_poll(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test the unit as read while writing counts, not the last poll."""
    assert state_of(hass, "climate.office").state == HVACMode.HEAT
    itm.units[0].running = False  # stopped at the wall controller
    await _call(
        hass,
        SERVICE_SET_HVAC_MODE,
        "climate.office",
        **{ATTR_HVAC_MODE: HVACMode.HEAT},
    )
    assert itm.units[0].running is True
    assert state_of(hass, "climate.office").state == HVACMode.HEAT


@pytest.mark.usefixtures("init_integration")
async def test_set_hvac_mode_already_in_that_mode_writes_nothing(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test a request that matches the unit opens no settings dialog."""
    before = len(itm.requests)
    await _call(hass, SERVICE_SET_HVAC_MODE, ROOM, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert not any(r["screen"] == 7 for r in itm.requests[before:])
    assert itm.units[1].mode == "Koelen"


@pytest.mark.usefixtures("init_integration")
async def test_set_hvac_mode_not_offered(hass: HomeAssistant) -> None:
    """Test a mode the unit does not offer is refused."""
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_SET_HVAC_MODE, ROOM, **{ATTR_HVAC_MODE: HVACMode.DRY})


@pytest.mark.usefixtures("init_integration")
async def test_turn_on_and_off(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test stopping and starting a unit."""
    await _call(hass, SERVICE_TURN_OFF, ROOM)
    assert itm.units[1].running is False
    assert state_of(hass, ROOM).state == HVACMode.OFF
    await _call(hass, SERVICE_TURN_ON, ROOM)
    assert itm.units[1].running is True
    assert state_of(hass, ROOM).state == HVACMode.COOL


@pytest.mark.usefixtures("init_integration")
async def test_set_fan_mode(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test changing the fan speed."""
    await _call(hass, SERVICE_SET_FAN_MODE, ROOM, **{ATTR_FAN_MODE: "high"})
    assert state_of(hass, ROOM).attributes["fan_mode"] == "high"
    assert itm.session is None
    assert itm.dialog is None


@pytest.mark.usefixtures("init_integration")
async def test_change_shows_before_it_reaches_the_unit(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test a service call returns without waiting for the unit, a poll confirms."""
    itm.latency_logins = 1  # the change shows from the next login on
    await _call(hass, SERVICE_SET_FAN_MODE, ROOM, **{ATTR_FAN_MODE: "high"})
    assert itm.units[1].fan_icon == 3011  # still medium on the unit
    assert state_of(hass, ROOM).attributes["fan_mode"] == "high"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert itm.units[1].fan_icon == 3013
    assert state_of(hass, ROOM).attributes["fan_mode"] == "high"


@pytest.mark.usefixtures("init_integration")
async def test_network_error_in_command_is_a_ha_error(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test a command that can't reach the iTM."""
    itm.fail_next = aiohttp.ClientConnectionError("gone")
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, SERVICE_SET_FAN_MODE, ROOM, **{ATTR_FAN_MODE: "low"})
    assert err.value.translation_key == "cannot_connect"


@pytest.mark.usefixtures("init_integration")
async def test_setpoint_outside_the_current_range(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test a setpoint the unit no longer accepts, e.g. after a mode change.

    The range read while writing is shown right away.
    """
    assert state_of(hass, ROOM).attributes["min_temp"] == 20
    itm.units[1].setpoint_range = (21, 22, 23, 24)  # the iTM changed the range
    itm.units[1].setpoint = 22
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_SET_TEMPERATURE, ROOM, **{ATTR_TEMPERATURE: 20})
    assert err.value.translation_key == "setpoint_out_of_range"
    assert err.value.translation_placeholders == {
        "unit": "Meeting room",
        "temperature": "20",
        "min": "21",
        "max": "24",
    }
    attributes = state_of(hass, ROOM).attributes
    assert (attributes["min_temp"], attributes["max_temp"]) == (21, 24)
    assert itm.units[1].setpoint == 22  # unchanged


@pytest.mark.usefixtures("init_integration")
async def test_refused_command_is_a_ha_error(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test a command the iTM refuses."""
    itm.units[1].modes = ("Ventilator", "Koelen")  # no longer offers heating
    with pytest.raises(HomeAssistantError) as err:
        await _call(
            hass, SERVICE_SET_HVAC_MODE, ROOM, **{ATTR_HVAC_MODE: HVACMode.HEAT}
        )
    assert err.value.translation_key == "command_failed"
    assert err.value.translation_placeholders is not None
    assert "not offered" in err.value.translation_placeholders["error"]
