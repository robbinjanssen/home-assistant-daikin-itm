"""Tests for showing changes before the iTM confirmed them (optimistic updates)."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import aiohttp
import pytest
from daikin_itm import ItmClient
from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    HVACMode,
)
from homeassistant.components.climate import DOMAIN as CLIMATE
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.exceptions import HomeAssistantError

from . import state_of

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .fake_itm import FakeITM

ROOM = "climate.meeting_room"  # 1:1-04, running, cooling, fan medium


async def _set_fan_mode(hass: HomeAssistant, fan_mode: str) -> None:
    await hass.services.async_call(
        CLIMATE,
        SERVICE_SET_FAN_MODE,
        {ATTR_ENTITY_ID: ROOM, ATTR_FAN_MODE: fan_mode},
        blocking=True,
    )


@pytest.mark.usefixtures("init_integration")
async def test_change_shows_before_it_is_written(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test the entity shows a change while the iTM is still being written."""
    gate = asyncio.Event()

    async def slow_post(_self: ItmClient, body: str) -> str:
        await gate.wait()
        return itm.handle(body)

    with patch.object(ItmClient, "_post", slow_post):
        call = hass.async_create_task(_set_fan_mode(hass, "high"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not call.done()
        assert state_of(hass, ROOM).attributes["fan_mode"] == "high"
        gate.set()
        await call
    assert itm.units[1].fan_icon == 3013
    assert state_of(hass, ROOM).attributes["fan_mode"] == "high"


@pytest.mark.usefixtures("init_integration")
async def test_change_the_itm_does_not_accept_is_undone(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test the entity goes back when the write fails."""
    itm.fail_next = aiohttp.ClientConnectionError("gone")
    with pytest.raises(HomeAssistantError):
        await _set_fan_mode(hass, "high")
    assert state_of(hass, ROOM).attributes["fan_mode"] == "medium"


async def test_poll_before_the_change_reaches_the_unit_does_not_undo_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test a poll that still reads the old state keeps showing the change."""
    coordinator = init_integration.runtime_data
    itm.latency_logins = 2  # not on the unit for the next poll yet
    await hass.services.async_call(
        CLIMATE,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: ROOM, ATTR_HVAC_MODE: HVACMode.HEAT},
        blocking=True,
    )
    await coordinator.async_refresh()
    assert itm.units[1].mode == "Koelen"
    assert state_of(hass, ROOM).state == HVACMode.HEAT

    await coordinator.async_refresh()  # now the unit shows it
    assert itm.units[1].mode == "Verwarmen"
    assert state_of(hass, ROOM).state == HVACMode.HEAT
    assert not coordinator._pending


async def test_change_that_never_shows_is_dropped(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    itm: FakeITM,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a change the unit never shows is no longer shown after a while."""
    coordinator = init_integration.runtime_data
    itm.latency_logins = 10_000
    await _set_fan_mode(hass, "high")
    await coordinator.async_refresh()
    assert state_of(hass, ROOM).attributes["fan_mode"] == "high"

    freezer.tick(timedelta(seconds=31))
    await coordinator.async_refresh()
    assert state_of(hass, ROOM).attributes["fan_mode"] == "medium"
    assert not coordinator._pending
