"""Fixtures for the Home Assistant integration tests, against the simulated iTM."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from daikin_itm import ItmClient
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daikin_itm.const import DOMAIN

from . import DATA
from .fake_itm import FakeITM

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,  # pylint: disable=unused-argument
) -> None:
    """Enable custom integrations."""


@pytest.fixture
def itm() -> Generator[FakeITM]:
    """Let the integration's client talk to a simulated iTM instead of the network."""
    fake = FakeITM()
    orig_init = ItmClient.__init__

    def init(self: ItmClient, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        self.commit_delay = 0
        self.min_write_interval = 0
        self.confirm_interval = 0

    async def post(_self: ItmClient, body: str) -> str:
        return fake.handle(body)

    with (
        patch.object(ItmClient, "__init__", init),
        patch.object(ItmClient, "_post", post),
    ):
        yield fake


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the simulated iTM."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Daikin iTM (10.0.0.1)",
        data=DATA,
        unique_id=DATA["host"],
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    itm: FakeITM,  # noqa: ARG001 # pylint: disable=redefined-outer-name,unused-argument
    mock_config_entry: MockConfigEntry,  # pylint: disable=redefined-outer-name
) -> MockConfigEntry:
    """Set up the integration with the simulated iTM."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
