"""Tests for the Daikin iTM Home Assistant integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

    from .fake_itm import FakeITM

DATA = {
    "host": "10.0.0.1",
    "username": "user",
    "password": "secret",
    "zone_path": "Building/Indoor units",
}

# The session token store, per iTM host.
SESSION_KEY = "daikin_itm.10_0_0_1.session"


def leave_session_behind(
    itm: FakeITM, hass_storage: dict[str, Any], token: str = "c" * 32
) -> None:
    """Simulate a crash while logged in, after the token was saved."""
    itm.session = token
    hass_storage[SESSION_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": SESSION_KEY,
        "data": {"token": token},
    }


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """Return the state of an entity that must exist."""
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state
