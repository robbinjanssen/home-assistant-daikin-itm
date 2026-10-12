"""Tests for setting up, polling and removing the integration."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.daikin_itm.const import DOMAIN
from custom_components.daikin_itm.coordinator import HassSessionStore
from custom_components.daikin_itm.diagnostics import (
    async_get_config_entry_diagnostics,
)

from . import DATA, SESSION_KEY, leave_session_behind, state_of

if TYPE_CHECKING:
    from typing import Any

    import pytest
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.typing import WebSocketGenerator

    from .fake_itm import FakeITM

ROOM = "climate.meeting_room"


async def test_unload(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Test unloading the entry."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED


async def test_setup_retries_when_itm_unreachable(
    hass: HomeAssistant, itm: FakeITM, mock_config_entry: MockConfigEntry
) -> None:
    """Test the entry is retried when the iTM can't be read."""
    itm.fail_next = TimeoutError()
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_stable_ids_and_devices(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test entities and devices are identified by entry and bus address."""
    entity = er.async_get(hass).async_get(ROOM)
    assert entity is not None
    assert entity.unique_id == f"{init_integration.entry_id}_1:1-04"
    devices = dr.async_get(hass)
    assert entity.device_id is not None
    device = devices.async_get(entity.device_id)
    assert isinstance(device, dr.DeviceEntry)
    assert (device.name, device.model) == ("Meeting room", "Indoor unit")
    assert device.via_device_id is not None
    hub = devices.async_get(device.via_device_id)
    assert isinstance(hub, dr.DeviceEntry)
    assert hub.identifiers == {(DOMAIN, init_integration.entry_id)}
    assert (hub.name, hub.model) == (
        "Daikin iTM (10.0.0.1)",
        "intelligent Touch Manager",
    )
    assert hub.configuration_url == "http://10.0.0.1"
    # no description in the iTM: named after the unit
    assert state_of(hass, "climate.1_1_09").attributes["friendly_name"] == "1:1-09"


async def test_renamed_unit_keeps_its_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test a unit renamed in the iTM keeps its entities, and commands use the name."""
    itm.units[1].name = "Board room"
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, ROOM).state == HVACMode.COOL
    entities = er.async_entries_for_config_entry(
        er.async_get(hass), init_integration.entry_id
    )
    assert len(entities) == 3 * 4
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": ROOM, "temperature": 23},
        blocking=True,
    )
    assert itm.units[1].setpoint == 23


async def test_new_unit_gets_entities(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test a unit added to the zone gets entities without a restart."""
    zone = itm.root.children[0].children[0]
    zone.children.append(type(itm.units[0])("1:1-12", point_id="113", address="1-12"))
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, "climate.1_1_12").state == HVACMode.HEAT


async def test_unit_disappearing_makes_entity_unavailable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    itm: FakeITM,
    hass_ws_client: WebSocketGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a unit that left the zone is unavailable, and its device can be removed."""
    zone = itm.root.children[0].children[0]
    zone.children.remove(itm.units[1])
    await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, ROOM).state == "unavailable"
    assert "Traceback" not in caplog.text

    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    devices = dr.async_get(hass)

    async def remove(identifier: str) -> Any:
        device = devices.async_get_device_by_identifier(
            (DOMAIN, identifier), init_integration.entry_id
        )
        assert device is not None
        await client.send_json_auto_id(
            {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": init_integration.entry_id,
                "device_id": device.id,
            }
        )
        return await client.receive_json()

    entry_id = init_integration.entry_id
    assert (await remove(f"{entry_id}_1:1-00"))["success"] is False
    assert (await remove(entry_id))["success"] is False
    assert (await remove(f"{entry_id}_1:1-04"))["success"] is True


async def test_poll_tolerates_web_ui_session(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test the last data is kept while someone uses the web UI."""
    itm.session = "f" * 32
    await init_integration.runtime_data.async_refresh()
    assert init_integration.runtime_data.last_update_success
    assert state_of(hass, ROOM).state == HVACMode.COOL


async def test_poll_gives_up_after_many_refused_logins(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test entities become unavailable, then an issue is raised, as logins fail."""
    coordinator = init_integration.runtime_data
    issue_id = f"login_rejected_{init_integration.entry_id}"
    itm.session = "f" * 32
    with (
        patch(
            "custom_components.daikin_itm.coordinator.MAX_TOLERATED_LOGIN_REJECTIONS", 1
        ),
        patch("custom_components.daikin_itm.coordinator.LOGIN_REJECTED_ISSUE_AFTER", 3),
    ):
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert state_of(hass, ROOM).state == "unavailable"
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
        await coordinator.async_refresh()
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_placeholders == {"host": "10.0.0.1", "minutes": "3"}

        itm.session = None
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_polling_does_not_write_the_session_to_disk(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Test a normal poll never touches .storage."""
    for _ in range(3):
        await init_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert SESSION_KEY not in hass_storage


async def test_session_left_behind_is_released_at_startup(
    hass: HomeAssistant,
    itm: FakeITM,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Test a session from before a crash is logged out first."""
    mock_config_entry.add_to_hass(hass)
    leave_session_behind(itm, hass_storage)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, ROOM).state == HVACMode.COOL
    assert itm.session is None
    assert SESSION_KEY not in hass_storage


async def test_long_session_keeps_the_saved_token_current(
    hass: HomeAssistant,
    itm: FakeITM,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Test every token is saved once a session outlasts the save delay.

    A crash at any later point can then still be released after the restart.
    """
    store = HassSessionStore(hass, DATA["host"])
    assert await store.load() is None
    await store.save("a" * 32)  # logged in
    await store.save("b" * 32)
    assert SESSION_KEY not in hass_storage  # a short session never touches the disk
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert hass_storage[SESSION_KEY]["data"]["token"] == "b" * 32
    for token in ("c" * 32, "d" * 32):  # every reply carries a new token
        await store.save(token)
        assert hass_storage[SESSION_KEY]["data"]["token"] == token

    # Home Assistant crashes here, without logging out. After the restart the first
    # poll releases the session.
    itm.session = "d" * 32
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert state_of(hass, ROOM).state == HVACMode.COOL
    assert itm.session is None
    assert SESSION_KEY not in hass_storage


async def test_removing_the_entry_removes_the_token(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    """Test the stored session token is removed with the entry."""
    hass_storage[SESSION_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": SESSION_KEY,
        "data": {},
    }
    await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()
    assert SESSION_KEY not in hass_storage


async def test_diagnostics(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the diagnostics leave out the address and credentials."""
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)
    assert diagnostics["entry"] == {
        **DATA,
        "host": "**REDACTED**",
        "username": "**REDACTED**",
        "password": "**REDACTED**",
    }
    assert diagnostics["units"]["1:1-04"]["setpoint"] == 20
    assert diagnostics["capabilities"]["1:1-04"]["description"] == "Meeting room"
    assert "secret" not in str(diagnostics)
