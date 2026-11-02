"""Fixtures for the Home Assistant integration tests, against the simulated iTM."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from daikin_itm import ItmClient
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daikin_itm.const import DOMAIN
from custom_components.daikin_itm.weather import OPEN_METEO_URL

from . import DATA
from .fake_itm import FakeITM

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,  # pylint: disable=unused-argument
) -> None:
    """Enable custom integrations."""


def open_meteo_reply(current: float, hourly: float) -> dict[str, Any]:
    """Return an Open-Meteo reply: the temperature now, and a steady one every hour."""
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    times = [start + timedelta(hours=h) for h in range(-30, 18)]
    return {
        "current": {
            "time": start.strftime("%Y-%m-%dT%H:%M"),
            "temperature_2m": current,
        },
        "hourly": {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
            "temperature_2m": [hourly] * len(times),
        },
    }


@pytest.fixture(autouse=True)
def open_meteo(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Answer the outdoor temperature requests: 9.5 °C now, 8 °C on average."""
    aioclient_mock.get(OPEN_METEO_URL, json=open_meteo_reply(9.5, 8.0))
    return aioclient_mock


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
    await hass.async_block_till_done(wait_background_tasks=True)  # outdoor temperature
    return mock_config_entry
