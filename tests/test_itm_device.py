"""Tests for the iTM's own settings: setback, the touch panel, status and clock."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from daikin_itm import ItmClient
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    EntityCategory,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.daikin_itm.const import DOMAIN
from custom_components.daikin_itm.diagnostics import (
    async_get_config_entry_diagnostics,
)

from . import state_of
from .test_settings import entity_id

if TYPE_CHECKING:
    from datetime import datetime

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .fake_itm import FakeITM


async def _call(hass: HomeAssistant, domain: str, service: str, **data: object) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


def _category(hass: HomeAssistant, entity: str) -> EntityCategory | None:
    entry = er.async_get(hass).async_get(entity)
    assert entry is not None
    return entry.entity_category


async def test_setback(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test a number per setback setting, within the values the iTM offers."""
    high_up = entity_id(hass, init_integration, "setback_high_up")
    state = state_of(hass, high_up)
    assert state.state == "3"
    assert (state.attributes["min"], state.attributes["max"]) == (1, 7)
    assert state.attributes["unit_of_measurement"] == "°C"
    assert "device_class" not in state.attributes  # a difference, not a temperature
    assert _category(hass, high_up) is EntityCategory.CONFIG
    recovery = state_of(
        hass, entity_id(hass, init_integration, "setback_recovery_heat")
    )
    assert (recovery.state, recovery.attributes["max"]) == ("1", 6)

    await _call(hass, "number", "set_value", **{ATTR_ENTITY_ID: high_up, "value": 5})
    assert itm.setback["high_up"] == 5
    assert state_of(hass, high_up).state == "5"


async def test_touch_panel_numbers(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the backlight and volumes, as the iTM reads them back."""
    backlight = entity_id(hass, init_integration, "panel_backlight")
    state = state_of(hass, backlight)
    assert state.state == "7"
    assert (state.attributes["min"], state.attributes["max"]) == (1, 8)
    assert (
        state_of(hass, entity_id(hass, init_integration, "panel_buzzer_volume")).state
        == "1"
    )
    assert _category(hass, backlight) is EntityCategory.CONFIG

    await _call(hass, "number", "set_value", **{ATTR_ENTITY_ID: backlight, "value": 4})
    assert state_of(hass, backlight).state == "4"


async def test_buzzer_duration(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the buzzer duration: minutes, or until it is stopped."""
    duration = entity_id(hass, init_integration, "panel_buzzer_duration")
    state = state_of(hass, duration)
    assert state.state == "continuous"
    assert state.attributes["options"] == ["1", "3", "5", "continuous"]

    await _call(
        hass, "select", "select_option", **{ATTR_ENTITY_ID: duration, "option": "3"}
    )
    assert state_of(hass, duration).state == "3"
    await _call(
        hass,
        "select",
        "select_option",
        **{ATTR_ENTITY_ID: duration, "option": "continuous"},
    )
    assert state_of(hass, duration).state == "continuous"


async def test_screensaver(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the screensaver's mode and whether an error ends it."""
    mode = entity_id(hass, init_integration, "panel_screensaver_mode")
    assert state_of(hass, mode).state == "backlight_off"
    await _call(
        hass, "select", "select_option", **{ATTR_ENTITY_ID: mode, "option": "screen_2"}
    )
    assert state_of(hass, mode).state == "screen_2"

    on_error = entity_id(hass, init_integration, "screensaver_off_on_error")
    assert state_of(hass, on_error).state == STATE_OFF
    await _call(hass, "switch", SERVICE_TURN_ON, **{ATTR_ENTITY_ID: on_error})
    assert state_of(hass, on_error).state == STATE_ON


async def test_confirm_dialogs(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the confirm dialogs switch shows the setting as read back.

    The simulated iTM shows the confirm dialogs screen without its writes; the
    library tests those against its own simulation.
    """
    switch = entity_id(hass, init_integration, "confirm_dialogs")
    assert state_of(hass, switch).state == STATE_OFF
    assert _category(hass, switch) is EntityCategory.CONFIG
    with patch.object(
        ItmClient, "set_confirm_dialogs", AsyncMock(return_value=True)
    ) as write:
        await _call(hass, "switch", SERVICE_TURN_ON, **{ATTR_ENTITY_ID: switch})
    write.assert_awaited_once_with(enabled=True)
    assert state_of(hass, switch).state == STATE_ON


async def test_remote_management_and_firmware(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the remote management sensor and the firmware on the iTM's device."""
    remote = entity_id(hass, init_integration, "remote_management")
    state = state_of(hass, remote)
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "connectivity"
    assert state.attributes["status"] == "Connect"
    assert _category(hass, remote) is EntityCategory.DIAGNOSTIC

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, init_integration.entry_id), init_integration.entry_id
    )
    assert device is not None
    assert device.sw_version == "1.00.00 (Jan 01 2025, 00:00:00)"


async def test_sync_clock(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    itm: FakeITM,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test the clock is set to Home Assistant's local time."""
    freezer.move_to("2026-03-04 05:06:07+00:00")
    now: datetime = dt_util.now()
    button = entity_id(hass, init_integration, "sync_clock")
    assert _category(hass, button) is EntityCategory.CONFIG
    await _call(hass, "button", "press", **{ATTR_ENTITY_ID: button})
    assert itm.clock_text == now.strftime("%d/%m/%Y %H:%M:%S")


async def test_diagnostics_hide_the_remote_management_address(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the remote management server's address is redacted."""
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)
    remote = diagnostics["settings"]["config"]["system"]["remote_management"]
    assert remote["ip_address"] == "**REDACTED**"
    assert "198.51.100.7" not in str(diagnostics)
