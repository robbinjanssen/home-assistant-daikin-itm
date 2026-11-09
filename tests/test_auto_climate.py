"""Tests for auto mode in Home Assistant: climate, sensors, switch, logbook, options."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import aiohttp
import pytest
from daikin_itm import ItmClient
from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate import DOMAIN as CLIMATE
from homeassistant.components.switch import DOMAIN as SWITCH
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_LATITUDE,
    CONF_LOCATION,
    CONF_LONGITUDE,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.daikin_itm import logbook
from custom_components.daikin_itm.const import EVENT_AUTO_MODE
from custom_components.daikin_itm.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.daikin_itm.weather import OPEN_METEO_URL, parse_forecast

from . import state_of
from .conftest import open_meteo_reply

if TYPE_CHECKING:
    from collections.abc import Callable

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

    from .fake_itm import FakeITM

ROOM = "climate.ac_05"  # 1:1-04, running, cooling at 20 °C, room 21 °C
SENSOR = "sensor.ac_05_auto_mode"
VENTILATE = "switch.ac_05_ventilate_in_auto_mode"
OUTDOOR = "sensor.daikin_itm_10_0_0_1_outdoor_temperature"


@pytest.fixture(autouse=True)
def cooling_at_20(itm: FakeITM) -> None:
    """Let the unit under test cool at 20 °C, allowing 20 to 25 °C."""
    itm.units[1].setpoint = 20
    itm.units[1].setpoint_range = (20, 21, 22, 23, 24, 25)


async def _call(
    hass: HomeAssistant, service: str, entity_id: str = ROOM, /, **data: object
) -> None:
    domain = SWITCH if entity_id.startswith("switch.") else CLIMATE
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


async def _start(hass: HomeAssistant, target: int = 21) -> None:
    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        **{ATTR_TEMPERATURE: target, ATTR_HVAC_MODE: HVACMode.AUTO},
    )


async def _minutes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int
) -> None:
    """Let time pass, one poll a minute."""
    for _ in range(minutes):
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("init_integration")
async def test_entities(hass: HomeAssistant) -> None:
    """Test the auto mode entities, and the outdoor temperature."""
    assert HVACMode.AUTO in state_of(hass, ROOM).attributes["hvac_modes"]
    sensor = state_of(hass, SENSOR)
    assert sensor.state == "off"
    assert sensor.attributes["reason"] is None
    assert state_of(hass, VENTILATE).state == "on"
    outdoor = state_of(hass, OUTDOOR)
    assert outdoor.state == "9.5"
    assert outdoor.attributes["average_24h"] == 8.0


@pytest.mark.usefixtures("init_integration")
async def test_start(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test starting auto mode: heating in autumn, then the target as setpoint."""
    events = async_capture_events(hass, EVENT_AUTO_MODE)
    await _start(hass)
    unit = itm.units[1]
    assert (unit.mode, unit.setpoint) == ("Heat", 20)
    await _minutes(hass, freezer, 1)  # the read that shows heating sets the target
    assert unit.setpoint == 21
    state = state_of(hass, ROOM)
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_TEMPERATURE] == 21
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING
    sensor = state_of(hass, SENSOR)
    assert sensor.state == "heat"
    assert sensor.attributes["reason"] == "started"
    assert sensor.attributes["target"] == 21
    assert sensor.attributes["outdoor_temperature"] == 8.0
    assert sensor.attributes["changes_24h"] == 2  # the mode and the setpoint
    assert [e.data["reason"] for e in events] == ["started"]
    assert events[0].data["mode"] == "heat"


@pytest.mark.usefixtures("init_integration")
async def test_start_with_the_unit_setpoint(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test auto mode keeps the setpoint of a running unit, here by ventilating."""
    await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    assert itm.units[1].mode == "Fan"  # 21 °C is past the target of 20 °C
    assert state_of(hass, ROOM).attributes[ATTR_TEMPERATURE] == 20
    assert state_of(hass, SENSOR).state == "fan"
    await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.AUTO})
    assert state_of(hass, SENSOR).attributes["changes_24h"] == 1  # already on


async def test_set_temperature_with_auto_needs_auto_mode(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test set_temperature with auto mode on a unit that has none is refused."""
    itm.units[1].modes = ("Fan", "Cool")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await _start(hass)
    assert err.value.translation_key == "mode_not_supported"
    assert itm.units[1].mode == "Cool"


async def test_no_ventilation_without_fan_mode(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test auto mode heats past the target on a unit without fan mode."""
    itm.units[1].modes = ("Cool", "Heat")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(VENTILATE) is None
    await _start(hass, target=20)  # 21 °C would ventilate
    assert itm.units[1].mode == "Heat"
    assert state_of(hass, SENSOR).state == "heat"


async def test_unit_without_heating_has_no_auto_mode(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test auto mode needs heating and cooling."""
    itm.units[1].modes = ("Fan", "Cool")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert HVACMode.AUTO not in state_of(hass, ROOM).attributes["hvac_modes"]
    assert hass.states.get(SENSOR) is None
    assert hass.states.get(VENTILATE) is None


@pytest.mark.usefixtures("init_integration")
async def test_ventilates_when_warm_and_heats_again(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test the room getting warm switches to fan, and cooling down back to heat."""
    await _start(hass)
    unit = itm.units[1]
    unit.temp = 21.8
    await _minutes(hass, freezer, 10)
    sensor = state_of(hass, SENSOR)
    assert sensor.attributes["waiting"] == "history"
    await _minutes(hass, freezer, 15)
    sensor = state_of(hass, SENSOR)
    assert (sensor.attributes["wanted"], sensor.attributes["waiting"]) == (
        "fan",
        "active_time",
    )
    await _minutes(hass, freezer, 10)
    assert unit.mode == "Fan"
    sensor = state_of(hass, SENSOR)
    assert sensor.state == "fan"
    assert sensor.attributes["reason"] == "target_reached"
    assert state_of(hass, ROOM).attributes[ATTR_HVAC_ACTION] == HVACAction.FAN
    assert state_of(hass, ROOM).state == HVACMode.AUTO

    unit.temp = 20.2
    await _minutes(hass, freezer, 25)
    assert unit.mode == "Heat"
    assert state_of(hass, SENSOR).attributes["reason"] == "below_target"


@pytest.mark.usefixtures("init_integration")
async def test_ventilation_turned_off(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test turning ventilation off while ventilating heats again right away."""
    await _start(hass, target=20)  # 21 °C: ventilate
    assert itm.units[1].mode == "Fan"
    await _minutes(hass, freezer, 1)
    await _call(hass, "turn_off", VENTILATE)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state_of(hass, VENTILATE).state == "off"
    assert itm.units[1].mode == "Heat"
    assert itm.units[1].setpoint == 20
    assert state_of(hass, SENSOR).attributes["reason"] == "ventilation_off"
    await _call(hass, "turn_on", VENTILATE)
    assert state_of(hass, VENTILATE).state == "on"


@pytest.mark.usefixtures("init_integration")
async def test_changed_at_the_unit_stops_auto_mode(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test a mode chosen at the remote controller ends auto mode."""
    await _start(hass)
    itm.units[1].mode = "Cool"
    await _minutes(hass, freezer, 2)
    assert state_of(hass, ROOM).state == HVACMode.COOL
    sensor = state_of(hass, SENSOR)
    assert sensor.state == "off"
    assert sensor.attributes["reason"] == "overridden"


@pytest.mark.usefixtures("init_integration")
async def test_stopped_at_the_unit_stops_auto_mode(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test a unit stopped elsewhere ends auto mode."""
    await _start(hass)
    itm.units[1].running = False
    await _minutes(hass, freezer, 2)
    assert state_of(hass, ROOM).state == HVACMode.OFF
    assert state_of(hass, SENSOR).attributes["reason"] == "overridden"


@pytest.mark.usefixtures("init_integration")
async def test_setpoint_changed_at_the_unit_becomes_the_target(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test a setpoint chosen at the remote controller is the new target."""
    await _start(hass)
    await _minutes(hass, freezer, 2)
    itm.units[1].setpoint = 23
    await _minutes(hass, freezer, 1)
    state = state_of(hass, ROOM)
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_TEMPERATURE] == 23
    assert state_of(hass, SENSOR).attributes["reason"] == "target_changed"


async def test_target_set_after_restart(
    hass: HomeAssistant,
    itm: FakeITM,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test auto mode sets the target after a restart when the setpoint differs."""
    itm.units[1].mode = "Heat"
    itm.units[1].setpoint = 23
    await _restart_in_auto(hass, mock_config_entry, target=21)
    await _minutes(hass, freezer, 1)
    assert itm.units[1].setpoint == 21
    assert state_of(hass, ROOM).state == HVACMode.AUTO


@pytest.mark.usefixtures("init_integration")
async def test_set_temperature_in_auto_mode(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test a new temperature changes the target and the unit's setpoint."""
    await _start(hass)
    await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 22})
    assert itm.units[1].setpoint == 22
    assert state_of(hass, ROOM).attributes[ATTR_TEMPERATURE] == 22
    assert state_of(hass, ROOM).state == HVACMode.AUTO
    sensor = state_of(hass, SENSOR)
    assert (sensor.attributes["reason"], sensor.attributes["target"]) == (
        "target_changed",
        22,
    )


@pytest.mark.usefixtures("init_integration")
async def test_another_mode_stops_auto_mode(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test choosing a mode, or off, ends auto mode."""
    await _start(hass)
    await _call(hass, SERVICE_TURN_ON)  # runs already
    assert state_of(hass, ROOM).state == HVACMode.AUTO
    await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.COOL})
    assert itm.units[1].mode == "Cool"
    sensor = state_of(hass, SENSOR)
    assert (sensor.state, sensor.attributes["reason"]) == ("off", "manual")

    await _start(hass)
    await _call(hass, SERVICE_TURN_OFF)
    assert itm.units[1].running is False
    assert state_of(hass, ROOM).state == HVACMode.OFF
    assert state_of(hass, SENSOR).state == "off"


@pytest.mark.usefixtures("init_integration")
async def test_failed_change(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test a change the iTM does not accept is recorded and retried later."""
    await _start(hass)
    itm.units[1].temp = 21.8
    await _minutes(hass, freezer, 29)
    itm.units[1].modes = ("Cool", "Heat")  # the iTM refuses fan for a while
    await _minutes(hass, freezer, 1)
    itm.units[1].modes = ("Fan", "Cool", "Heat")
    assert itm.units[1].mode == "Heat"
    sensor = state_of(hass, SENSOR)
    assert sensor.state == "heat"
    assert sensor.attributes["reason"] == "write_failed"
    await _minutes(hass, freezer, 21)
    assert itm.units[1].mode == "Fan"


async def _restart_in_auto(
    hass: HomeAssistant, entry: MockConfigEntry, target: int
) -> None:
    """Set up the integration with auto mode heating as stored before a restart."""
    stored = {
        "enabled": True,
        "target": target,
        "ventilate": False,
        "mode": "heat",
        "side": "heat",
        "switched_at": "2026-10-31T06:00:00+00:00",
        "side_since": "2026-10-31T06:00:00+00:00",
        "switches": [],
        "last": None,
    }
    mock_restore_cache_with_extra_data(hass, [(State(ROOM, HVACMode.AUTO), stored)])
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_restored_after_restart(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test auto mode survives a restart."""
    itm.units[1].mode = "Heat"
    itm.units[1].setpoint = 22
    await _restart_in_auto(hass, mock_config_entry, target=22)
    state = state_of(hass, ROOM)
    assert state.state == HVACMode.AUTO
    assert state.attributes[ATTR_TEMPERATURE] == 22
    assert state_of(hass, SENSOR).state == "heat"
    assert state_of(hass, VENTILATE).state == "off"
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    assert diagnostics["auto_mode"]["1:1-04"]["target"] == 22
    assert diagnostics["outdoor_temperature"] == {"current": 9.5, "mean_24h": 8.0}


@pytest.mark.usefixtures("init_integration")
async def test_logbook(hass: HomeAssistant) -> None:
    """Test the logbook describes a decision with its reason."""
    described: dict[str, Callable[[Event], dict[str, str]]] = {}

    def register(
        _domain: str, event_type: str, describe: Callable[[Event], dict[str, str]]
    ) -> None:
        described[event_type] = describe

    logbook.async_describe_events(hass, register)
    event: Event = Event(
        EVENT_AUTO_MODE,
        {
            ATTR_ENTITY_ID: ROOM,
            "mode": "cool",
            "reason": "too_warm",
            "target": 21,
            "temperature": 22.6,
            "outdoor": 15.2,
        },
    )
    assert described[EVENT_AUTO_MODE](event) == {
        "name": "AC 05",
        "message": (
            "Cool: too warm, changing over to cooling (Room temperature 22.6 °C, "
            "Target 21 °C, Outdoor temperature 15.2 °C)"
        ),
        "entity_id": ROOM,
    }
    event = Event(
        EVENT_AUTO_MODE,
        {
            ATTR_ENTITY_ID: "climate.gone",
            "mode": "off",
            "reason": "manual",
            "target": 21,
            "temperature": None,
            "outdoor": None,
        },
    )
    assert described[EVENT_AUTO_MODE](event)["message"] == (
        "Off: another mode was chosen (Target 21 °C)"
    )


async def test_without_outdoor_temperature(
    hass: HomeAssistant,
    itm: FakeITM,
    mock_config_entry: MockConfigEntry,
    open_meteo: AiohttpClientMocker,
) -> None:
    """Test auto mode works when Open-Meteo can't be reached."""
    open_meteo.clear_requests()
    open_meteo.get(OPEN_METEO_URL, exc=aiohttp.ClientConnectionError("offline"))
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state_of(hass, OUTDOOR).state == STATE_UNAVAILABLE
    await _start(hass)
    # No season known: the unit was cooling, and the room is at the target.
    assert itm.units[1].mode == "Cool"
    assert state_of(hass, SENSOR).attributes["outdoor_temperature"] is None


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"current": {"temperature_2m": "warm"}, "hourly": {}},
        {
            "current": {"temperature_2m": 9.0},
            "hourly": {"time": ["2020-01-01T00:00"], "temperature_2m": [1.0]},
        },
    ],
)
def test_unexpected_open_meteo_reply(reply: dict[str, Any]) -> None:
    """Test replies without the temperatures are refused."""
    with pytest.raises(ValueError, match=r"reply|24 hours"):
        parse_forecast(reply, State("a.b", "x").last_changed)


def test_open_meteo_reply_skips_missing_hours() -> None:
    """Test hours without a temperature don't count."""
    reply = open_meteo_reply(9.0, 6.0)
    reply["hourly"]["temperature_2m"][-20] = None
    now = State("a.b", "x").last_changed
    assert parse_forecast(reply, now).mean_24h == 6.0


async def test_options(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    open_meteo: AiohttpClientMocker,
) -> None:
    """Test choosing the location of the outdoor temperature."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    location = {CONF_LATITUDE: 51.93, CONF_LONGITUDE: 6.07}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LOCATION: location}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert init_integration.options[CONF_LOCATION] == location
    _method, url, _data, _headers = open_meteo.mock_calls[-1]
    assert (url.query["latitude"], url.query["longitude"]) == ("51.93", "6.07")


@pytest.mark.usefixtures("init_integration")
async def test_stopped_while_switching(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test a mode chosen while auto mode switches the unit wins."""
    await _start(hass)
    itm.units[1].temp = 21.8
    await _minutes(hass, freezer, 29)  # the next read switches to fan
    release = asyncio.Event()
    apply = ItmClient.apply
    calls = 0

    async def slow_apply(self: ItmClient, *args: Any, **kwargs: Any) -> Any:
        # Auto mode's write returns late, after the user chose another mode.
        nonlocal calls
        calls += 1
        result = await apply(self, *args, **kwargs)
        if calls == 1:
            await release.wait()
        return result

    with patch.object(ItmClient, "apply", slow_apply):
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        # The poll runs in the background, and its write blocks: wait for the write.
        for _ in range(1000):
            if calls:
                break
            await asyncio.sleep(0)
        assert calls == 1  # auto mode is switching to fan
        choose = hass.async_create_task(
            _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.COOL})
        )
        await asyncio.sleep(0)
        release.set()
        await choose
        await hass.async_block_till_done(wait_background_tasks=True)
    assert itm.units[1].mode == "Cool"
    assert state_of(hass, ROOM).state == HVACMode.COOL
    sensor = state_of(hass, SENSOR)
    assert (sensor.state, sensor.attributes["reason"]) == ("off", "manual")


@pytest.mark.usefixtures("init_integration")
async def test_target_kept_when_the_unit_allows_less(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test the nearest allowed setpoint is used without changing the target."""
    await _call(hass, "turn_off", VENTILATE)
    await _start(hass, target=20)  # 21 °C, cold outside: heating
    unit = itm.units[1]
    assert unit.mode == "Heat"
    unit.setpoint_range = (21, 22, 23, 24, 25)  # heating allows 21 °C and up
    unit.setpoint = 21
    await _minutes(hass, freezer, 3)
    assert unit.setpoint == 21
    assert state_of(hass, ROOM).attributes[ATTR_TEMPERATURE] == 20
    assert state_of(hass, SENSOR).attributes["reason"] == "started"


@pytest.mark.usefixtures("init_integration")
async def test_failed_target_change_is_tried_again(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test a target the iTM did not take is written later, not reverted."""
    await _start(hass)
    await _minutes(hass, freezer, 1)
    itm.fail_next = aiohttp.ClientConnectionError("gone")
    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 23})
    assert itm.units[1].setpoint == 21
    await _minutes(hass, freezer, 2)
    assert itm.units[1].setpoint == 23
    assert state_of(hass, ROOM).attributes[ATTR_TEMPERATURE] == 23


@pytest.mark.usefixtures("init_integration")
async def test_setpoint_changed_while_ventilating(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test a setpoint chosen at the remote controller while ventilating counts."""
    await _start(hass, target=20)  # 21 °C: ventilate
    await _minutes(hass, freezer, 1)
    itm.units[1].setpoint = 23
    await _minutes(hass, freezer, 1)
    assert state_of(hass, ROOM).attributes[ATTR_TEMPERATURE] == 23
    sensor = state_of(hass, SENSOR)
    assert (sensor.state, sensor.attributes["reason"]) == ("fan", "target_changed")


@pytest.mark.usefixtures("init_integration")
async def test_refused_logins_add_no_readings(
    hass: HomeAssistant, itm: FakeITM, freezer: FrozenDateTimeFactory
) -> None:
    """Test the old data shown while logins are refused is not taken as readings."""
    await _start(hass)
    await _minutes(hass, freezer, 1)
    events = async_capture_events(hass, EVENT_AUTO_MODE)
    unit = itm.units[1]
    unit.temp = 21.8
    itm.session = "d" * 32  # someone uses the web interface
    await _minutes(hass, freezer, 20)
    assert unit.mode == "Heat"
    assert not events  # no decisions, so no writes that fail
    itm.session = None
    await _minutes(hass, freezer, 14)
    assert unit.mode == "Heat"  # 15 minutes of new readings first
    await _minutes(hass, freezer, 2)
    assert unit.mode == "Fan"


async def test_outdoor_average_kept_for_a_while(
    init_integration: MockConfigEntry,
    open_meteo: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a failed Open-Meteo read keeps the last average for a few hours."""
    outdoor = init_integration.runtime_data.outdoor
    assert outdoor is not None
    open_meteo.clear_requests()
    open_meteo.get(OPEN_METEO_URL, exc=aiohttp.ClientConnectionError("offline"))
    await outdoor.async_refresh()
    assert not outdoor.last_update_success
    assert outdoor.mean_24h == 8.0
    freezer.tick(timedelta(hours=4))
    assert outdoor.mean_24h is None
