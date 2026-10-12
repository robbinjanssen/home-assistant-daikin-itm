"""Tests for the config flow."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daikin_itm.const import DOMAIN

from . import DATA, SESSION_KEY, leave_session_behind, state_of

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .fake_itm import FakeITM


async def test_config_flow(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test setting up an iTM."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Daikin iTM (10.0.0.1)"
    assert result["result"].unique_id == "10.0.0.1"
    assert itm.session is None


async def test_config_flow_login_rejected(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test a refused login."""
    itm.session = "f" * 32  # somebody is logged in to the web UI
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "login_rejected"}


async def test_config_flow_bad_zone(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test a zone path that does not exist lists what does."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**DATA, "zone_path": "Building/Nope"}
    )
    assert result["errors"] == {"zone_path": "zone_not_found"}
    assert result["description_placeholders"] == {
        "available": "Indoor units, Ventilation"
    }
    assert itm.session is None


async def test_config_flow_empty_zone(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test a zone without units."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**DATA, "zone_path": "Building/Ventilation"}
    )
    assert result["errors"] == {"zone_path": "no_units"}
    assert itm.session is None


async def test_config_flow_cannot_connect(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test an iTM that cannot be reached."""
    itm.fail_next = TimeoutError()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_config_flow_unexpected_error(hass: HomeAssistant, itm: FakeITM) -> None:
    """Test an unexpected error."""
    itm.fail_next = ValueError("boom")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["errors"] == {"base": "unknown"}


async def test_config_flow_same_itm_written_differently(
    hass: HomeAssistant,
    itm: FakeITM,  # noqa: ARG001 # pylint: disable=unused-argument
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test an iTM that is already set up is recognized by its address."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**DATA, "host": "http://10.0.0.1/"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure(
    hass: HomeAssistant, init_integration: MockConfigEntry, itm: FakeITM
) -> None:
    """Test changing the host and password keeps the entities."""
    itm.digest = hashlib.sha256(b"new").hexdigest()
    result = await init_integration.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**DATA, "host": "10.0.0.2", "password": "new"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert init_integration.unique_id == "10.0.0.2"
    assert init_integration.data["password"] == "new"
    assert init_integration.title == "Daikin iTM (10.0.0.2)"
    assert state_of(hass, "climate.meeting_room").state == "cool"


async def test_reconfigure_bad_zone(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test the reconfigure form shows errors."""
    result = await init_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**DATA, "zone_path": "Nope"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"zone_path": "zone_not_found"}
    assert result["description_placeholders"] == {"available": "Building"}


async def test_reconfigure_to_another_configured_itm(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test an entry can't be pointed at an iTM that has its own entry."""
    MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="10.0.0.3").add_to_hass(hass)
    result = await init_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**DATA, "host": "10.0.0.3"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert init_integration.unique_id == "10.0.0.1"


async def test_session_left_behind_by_the_config_flow_is_released(
    hass: HomeAssistant, itm: FakeITM, hass_storage: dict[str, Any]
) -> None:
    """Test a session left behind while validating, e.g. by a restart, is released."""
    leave_session_behind(itm, hass_storage)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert itm.session is None
    assert SESSION_KEY not in hass_storage


async def test_config_flow_no_zone_lists_the_zones(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test leaving the zone empty on an iTM with zones names them."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    data = {key: value for key, value in DATA.items() if key != "zone_path"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["errors"] == {"zone_path": "no_units"}
    assert result["description_placeholders"] == {"available": "Building"}
    assert itm.session is None


async def test_config_flow_units_at_the_top_level(
    hass: HomeAssistant, itm: FakeITM
) -> None:
    """Test an iTM with its units at the top level needs no zone path."""
    itm.root.children = itm.units
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    data = {key: value for key, value in DATA.items() if key != "zone_path"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert state_of(hass, "climate.meeting_room").state == "cool"
