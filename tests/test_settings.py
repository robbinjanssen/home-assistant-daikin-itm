"""Tests for the settings: units, schedules, changeover, overtime, indicators."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from daikin_itm import ItmClient, ItmCommandError
from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    ATTR_SWING_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_SWING_MODE,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.daikin_itm.const import DOMAIN
from custom_components.daikin_itm.diagnostics import (
    async_get_config_entry_diagnostics,
)

from . import state_of
from .fake_itm import FakeAnalog, FakeGroup

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .fake_itm import FakeITM

ROOM = "climate.ac_05"  # 1:1-04, running, cooling


def entity_id(hass: HomeAssistant, entry: MockConfigEntry, suffix: str) -> str:
    """Return the entity ID of an entity, by the end of its unique ID."""
    unique_id = f"{entry.entry_id}_{suffix}"
    for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if entity.unique_id == unique_id:
            return entity.entity_id
    msg = f"no entity {unique_id}"
    raise AssertionError(msg)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _call(hass: HomeAssistant, domain: str, service: str, **data: object) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


async def test_schedules(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test a switch per schedule program, on the iTM's device."""
    always = entity_id(hass, init_integration, "schedule_always")
    summer = entity_id(hass, init_integration, "schedule_program_g")
    assert state_of(hass, always).state == "on"
    state = state_of(hass, summer)
    assert state.state == "off"
    assert state.attributes["period"] == "01/06-31/08"
    entity = er.async_get(hass).async_get(summer)
    assert entity is not None
    assert entity.entity_category is EntityCategory.CONFIG

    await _call(hass, "switch", SERVICE_TURN_ON, **{ATTR_ENTITY_ID: summer})
    assert state_of(hass, summer).state == "on"
    await _call(hass, "switch", SERVICE_TURN_OFF, **{ATTR_ENTITY_ID: always})
    assert state_of(hass, always).state == "off"
    assert itm.session is None


async def test_schedule_removed_in_the_itm(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test a program deleted in the iTM makes its switch unavailable."""
    summer = entity_id(hass, init_integration, "schedule_program_g")
    del itm.schedules[1]
    await init_integration.runtime_data.settings.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, summer).state == STATE_UNAVAILABLE


async def test_changeover_groups(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test a switch per auto changeover group."""
    itm.groups.append(FakeGroup("Group A", enabled=True))
    await _setup(hass, mock_config_entry)
    group = entity_id(hass, mock_config_entry, "changeover_group_a")
    assert state_of(hass, group).state == "on"
    await _call(hass, "switch", SERVICE_TURN_OFF, **{ATTR_ENTITY_ID: group})
    assert itm.groups[0].enabled is False
    assert state_of(hass, group).state == "off"


async def test_overtime_duration(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test the overtime timer's duration."""
    overtime = entity_id(hass, init_integration, "overtime_duration")
    assert state_of(hass, overtime).state == "60"
    await _call(hass, "number", "set_value", **{ATTR_ENTITY_ID: overtime, "value": 90})
    assert state_of(hass, overtime).state == "90"
    assert itm.overtime_idx == 2


async def test_indicators(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test the main menu bar's indicators."""
    itm.energy_saving = True
    await _setup(hass, mock_config_entry)
    assert (
        state_of(hass, entity_id(hass, mock_config_entry, "energy_saving")).state
        == "on"
    )
    error = state_of(hass, entity_id(hass, mock_config_entry, "error"))
    assert (error.state, error.attributes["device_class"]) == ("off", "problem")
    stop = state_of(hass, entity_id(hass, mock_config_entry, "emergency_stop"))
    assert (stop.state, stop.attributes["device_class"]) == ("off", "safety")


async def test_remote_locks(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test the wall remote locks of a unit."""
    start_stop = entity_id(hass, init_integration, "1:1-04_start_stop_lock")
    setpoint = entity_id(hass, init_integration, "1:1-04_setpoint_lock")
    state = state_of(hass, start_stop)
    assert state.state == "allowed"
    assert state.attributes["options"] == ["allowed", "stop_only", "forbidden"]
    assert state_of(hass, setpoint).attributes["options"] == ["allowed", "forbidden"]

    await _call(
        hass,
        "select",
        "select_option",
        **{ATTR_ENTITY_ID: start_stop, "option": "stop_only"},
    )
    await _call(
        hass,
        "select",
        "select_option",
        **{ATTR_ENTITY_ID: setpoint, "option": "forbidden"},
    )
    assert (itm.units[1].lock_start_stop, itm.units[1].lock_setpoint) == (1, 2)
    assert state_of(hass, start_stop).state == "stop_only"
    assert state_of(hass, setpoint).state == "forbidden"
    assert itm.session is None


async def test_setpoint_limits(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test enabling a limit, and changing one end of it."""
    switch = entity_id(hass, init_integration, "1:1-04_cool_limit")
    maximum = entity_id(hass, init_integration, "1:1-04_cool_limit_max")
    assert state_of(hass, switch).state == "off"
    assert state_of(hass, maximum).state == STATE_UNAVAILABLE  # while disabled

    await _call(hass, "switch", SERVICE_TURN_ON, **{ATTR_ENTITY_ID: switch})
    assert state_of(hass, switch).state == "on"
    assert state_of(hass, maximum).state == "32.0"
    await _call(hass, "number", "set_value", **{ATTR_ENTITY_ID: maximum, "value": 28})
    assert itm.units[1].cool_limit == (True, 16.0, 28.0)
    assert state_of(hass, maximum).state == "28.0"
    minimum = entity_id(hass, init_integration, "1:1-04_cool_limit_min")
    await _call(hass, "number", "set_value", **{ATTR_ENTITY_ID: minimum, "value": 20})
    assert itm.units[1].cool_limit == (True, 20.0, 28.0)

    await _call(hass, "switch", SERVICE_TURN_OFF, **{ATTR_ENTITY_ID: switch})
    assert itm.units[1].cool_limit[0] is False
    assert state_of(hass, maximum).state == STATE_UNAVAILABLE


async def test_setback_presets(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test putting a unit in setback and back, through the climate's presets."""
    del init_integration
    state = state_of(hass, ROOM)
    assert state.attributes["preset_modes"] == ["none", "setback_low", "setback_high"]
    assert state.attributes[ATTR_PRESET_MODE] == "none"

    await _call(
        hass,
        "climate",
        SERVICE_SET_PRESET_MODE,
        **{ATTR_ENTITY_ID: ROOM, ATTR_PRESET_MODE: "setback_high"},
    )
    assert itm.units[1].setback == 2
    assert state_of(hass, ROOM).attributes[ATTR_PRESET_MODE] == "setback_high"

    await _call(
        hass,
        "climate",
        SERVICE_SET_PRESET_MODE,
        **{ATTR_ENTITY_ID: ROOM, ATTR_PRESET_MODE: "none"},
    )
    assert (itm.units[1].setback, itm.units[1].running) == (0, True)
    state = state_of(hass, ROOM)
    assert (state.state, state.attributes[ATTR_PRESET_MODE]) == (HVACMode.COOL, "none")


async def test_preset_none_leaves_a_stopped_unit_stopped(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test no setback on a stopped unit, which is not in setback, sends nothing."""
    del init_integration
    requests = len(itm.requests)
    await _call(
        hass,
        "climate",
        SERVICE_SET_PRESET_MODE,
        **{ATTR_ENTITY_ID: "climate.1_1_09", ATTR_PRESET_MODE: "none"},
    )
    assert len(itm.requests) == requests
    assert itm.units[2].running is False


async def test_airflow_direction(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test the airflow direction, as the climate's swing mode."""
    itm.units[1].airflow = 3101  # horizontal
    await _setup(hass, mock_config_entry)
    state = state_of(hass, ROOM)
    assert state.attributes[ATTR_SWING_MODE] == "position_0"
    assert "swing" in state.attributes["swing_modes"]
    assert "swing_modes" not in state_of(hass, "climate.office").attributes

    await _call(
        hass,
        "climate",
        SERVICE_SET_SWING_MODE,
        **{ATTR_ENTITY_ID: ROOM, ATTR_SWING_MODE: "swing"},
    )
    assert itm.units[1].airflow == 3111
    assert state_of(hass, ROOM).attributes[ATTR_SWING_MODE] == "swing"


async def test_analog_input_alarm(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test an analog input in the zone is an alarm sensor, not a unit."""
    itm.root.children[0].children[0].children.append(FakeAnalog("Alarm 2", alarm=True))
    await _setup(hass, mock_config_entry)
    alarm = state_of(hass, entity_id(hass, mock_config_entry, "analog_alarm_2"))
    assert (alarm.state, alarm.attributes["device_class"]) == ("on", "problem")
    assert hass.states.get("climate.alarm_2") is None


async def test_settings_failure_keeps_the_units(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test settings that can't be read leave the units working.

    Their entities are added once a later read works.
    """
    del itm
    with patch.object(
        ItmClient, "fetch_config", side_effect=ItmCommandError("not offered")
    ):
        await _setup(hass, mock_config_entry)
    assert state_of(hass, ROOM).state == HVACMode.COOL
    registry = er.async_get(hass)
    unique_id = f"{mock_config_entry.entry_id}_schedule_always"
    assert registry.async_get_entity_id("switch", DOMAIN, unique_id) is None

    await mock_config_entry.runtime_data.settings.async_refresh()
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("switch", DOMAIN, unique_id) is not None


async def test_settings_login_refused_keeps_the_last_settings(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test the settings stay while someone uses the web UI."""
    always = entity_id(hass, init_integration, "schedule_always")
    itm.session = "f" * 32  # somebody is logged in to the web UI
    await init_integration.runtime_data.settings.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, always).state == "on"


async def test_refused_setting_is_a_ha_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test a setting the iTM refuses."""
    always = entity_id(hass, init_integration, "schedule_always")
    itm.schedules.clear()  # deleted in the iTM since the last read
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "switch", SERVICE_TURN_OFF, **{ATTR_ENTITY_ID: always})
    assert err.value.translation_key == "command_failed"


async def test_diagnostics_hold_the_settings(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the settings are in the diagnostics."""
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diagnostics["settings_last_update_success"] is True
    settings = diagnostics["settings"]
    assert settings["config"]["schedules"][0]["name"] == "Always"
    assert settings["units"]["1:1-04"]["remote_locks"]["start_stop"] == "allowed"
    assert "secret" not in str(diagnostics)
