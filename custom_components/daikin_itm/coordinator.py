"""Polling coordinator: one short iTM session per update."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from daikin_itm import (
    Capabilities,
    ItmClient,
    ItmError,
    LoginRejected,
    SetpointOutOfRange,
    Unit,
)

from .api import unique_id_for_host
from .const import (
    CONFIRM_INTERVAL,
    DOMAIN,
    LOGGER,
    LOGIN_REJECTED_ISSUE_AFTER,
    MAX_TOLERATED_LOGIN_REJECTIONS,
    PENDING_TIMEOUT,
    SCAN_INTERVAL,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

type ItmConfigEntry = ConfigEntry[ItmCoordinator]


class HassSessionStore:
    """Keep the iTM page token in .storage.

    A session left behind by a crash or restart is then logged out on the next poll,
    instead of blocking the single web session until the iTM times it out.

    The token changes on every reply, but a poll's session lasts a second or two.
    Writing each token would mean dozens of .storage writes a minute (bad for SD
    cards), so a session's token is only written once the session has lasted
    SAVE_DELAY seconds, and from then on after every reply (a token on disk that is no
    longer current can't release anything). A crash during a normal poll therefore
    leaves the session to the iTM's own timeout.

    The store is per iTM, so the config flow and the config entry share it: a session
    left behind while validating the settings is released by the first poll.
    """

    SAVE_DELAY = 10

    def __init__(self, hass: HomeAssistant, host: str) -> None:
        """Initialize the store for an iTM.

        Args:
        ----
            hass: The HomeAssistant instance.
            host: The iTM's host setting.

        """
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.{slugify(unique_id_for_host(host))}.session"
        )
        self._token: str | None = None
        self._loaded = False
        self._written = False
        self._cancel_write: CALLBACK_TYPE | None = None

    async def load(self) -> str | None:
        """Return the saved token, reading it from disk only once."""
        if not self._loaded:  # afterwards memory is authoritative
            data = await self._store.async_load()
            self._token = data.get("token") if data else None
            self._written = self._token is not None
            self._loaded = True
        return self._token

    async def save(self, token: str | None) -> None:
        """Remember the current token, or that there is no session (None)."""
        self._loaded = True
        self._token = token
        if token is None:
            self.async_cancel_write()
            if self._written:
                await self._store.async_remove()
                self._written = False
        elif self._written:
            await self._async_write()  # a long session: keep the token on disk current
        elif self._cancel_write is None:
            self._cancel_write = async_call_later(
                self.hass, self.SAVE_DELAY, self._async_write
            )

    def async_cancel_write(self) -> None:
        """Cancel a pending write, for example when the entry is unloaded."""
        if self._cancel_write:
            self._cancel_write()
            self._cancel_write = None

    async def async_remove(self) -> None:
        """Remove the stored token, when the config entry is removed."""
        self.async_cancel_write()
        await self._store.async_remove()

    async def _async_write(self, _now: datetime | None = None) -> None:
        self._cancel_write = None
        if self._token is not None:
            await self._store.async_save({"token": self._token})
            self._written = True


@dataclasses.dataclass
class PendingChange:
    """A change that is shown before a poll confirmed it."""

    changes: dict[str, Any]
    # When it is no longer shown; None while it is being written.
    expires: datetime | None = None

    def shown_by(self, unit: Unit) -> bool:
        """Return if the unit as read shows the change."""
        return all(
            getattr(unit, field) == value for field, value in self.changes.items()
        )


class ItmCoordinator(DataUpdateCoordinator[dict[str, Unit]]):
    """Read all units of the configured zone.

    Data is keyed by each unit's stable key (its bus address, e.g. "1:1-04"), not by
    its name, so renaming a unit in the iTM keeps the same entities.

    The data is the units as last read, with the changes asked for since then on top
    (optimistic updates): a write takes seconds, and a change takes another 3-8
    seconds to reach the unit.
    """

    config_entry: ItmConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ItmConfigEntry, client: ItmClient
    ) -> None:
        """Initialize the coordinator.

        Args:
        ----
            hass: The HomeAssistant instance.
            entry: The config entry.
            client: The iTM client.

        """
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.capabilities: dict[str, Capabilities] = {}  # by key
        self._key_by_name: dict[str, str] = {}
        self._rejections = 0
        self._read: dict[str, Unit] = {}  # the units as last read
        self._pending: dict[str, list[PendingChange]] = {}  # by key
        # The device of the iTM itself, set up before the entities.
        self.hub_device_id: str | None = None

    @property
    def _issue_id(self) -> str:
        return f"login_rejected_{self.config_entry.entry_id}"

    async def _refresh_capabilities(self) -> None:
        # Identity, modes and fan control per unit; opens every unit's Details and
        # settings dialog.
        caps = await self.client.fetch_capabilities()
        self.capabilities = {c.key: c for c in caps.values()}
        self._key_by_name = {name: c.key for name, c in caps.items()}

    async def _async_update_data(self) -> dict[str, Unit]:
        """Read all units, in one session.

        Returns
        -------
            The units by key.

        Raises
        ------
            UpdateFailed: The iTM could not be read.

        """
        # Back to the normal interval after the poll that confirms a change.
        self.update_interval = SCAN_INTERVAL
        try:
            if not self.capabilities:
                await self._refresh_capabilities()
            units = await self.client.fetch_units()
            if set(units) - set(self._key_by_name):
                # A unit was added or renamed in the iTM: map names to keys again.
                await self._refresh_capabilities()
        except LoginRejected as err:
            return self._login_rejected(err)
        except (ItmError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if self._rejections >= LOGIN_REJECTED_ISSUE_AFTER:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self._rejections = 0
        self._read = {
            self._key_by_name[name]: unit
            for name, unit in units.items()
            if name in self._key_by_name
        }
        self._drop_settled_changes()
        return self._shown()

    def _shown(self) -> dict[str, Unit]:
        """Return the units as last read, with the pending changes on top."""
        shown = dict(self._read)
        for key, pending in self._pending.items():
            if key in shown:
                for change in pending:
                    shown[key] = dataclasses.replace(shown[key], **change.changes)
        return shown

    def _drop_settled_changes(self) -> None:
        """Stop showing changes the units show, or that took too long to show."""
        now = dt_util.utcnow()
        for key in list(self._pending):
            unit = self._read.get(key)
            self._pending[key] = [
                change
                for change in self._pending[key]
                if unit is not None
                and (
                    change.expires is None  # still being written
                    or (now < change.expires and not change.shown_by(unit))
                )
            ]
            if not self._pending[key]:
                del self._pending[key]

    def _login_rejected(self, err: LoginRejected) -> dict[str, Unit]:
        # Usually someone is logged in to the web UI; keep showing the last known
        # state for a while.
        self._rejections += 1
        if self._rejections == LOGIN_REJECTED_ISSUE_AFTER:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="login_rejected",
                translation_placeholders={
                    "host": self.config_entry.data[CONF_HOST],
                    "minutes": str(
                        int(LOGIN_REJECTED_ISSUE_AFTER * SCAN_INTERVAL.seconds / 60)
                    ),
                },
            )
        if self.data and self._rejections <= MAX_TOLERATED_LOGIN_REJECTIONS:
            LOGGER.debug(
                "iTM login refused (%s/%s), keeping last data",
                self._rejections,
                MAX_TOLERATED_LOGIN_REJECTIONS,
            )
            return self._shown()
        raise UpdateFailed(
            translation_domain=DOMAIN, translation_key="login_rejected"
        ) from err

    def display_name(self, key: str) -> str:
        """Return a unit's name for people.

        The description from the iTM's Details screen (e.g. "Meeting room") is
        far more readable than the unit name ("1:1-11"); falls back to the name when
        it is empty.

        Args:
        ----
            key: The unit's key.

        Returns:
        -------
            The description, the unit name, or the key.

        """
        caps = self.capabilities.get(key)
        if caps and caps.description:
            return caps.description
        unit = self.data.get(key) if self.data else None
        return unit.name if unit else key

    async def async_write(
        self,
        key: str,
        write: Callable[[str], Awaitable[Unit | None]],
        **changes: Any,
    ) -> None:
        """Change a unit, showing the change right away.

        The entities show the change before it is written (optimistic update), and
        go back if the iTM does not accept it. The write returns once the iTM
        accepted the change, without waiting the 3-8 seconds until the unit shows it;
        the next poll, which confirms it, runs soon.

        Args:
        ----
            key: The unit's key.
            write: Sends the change, given the unit's current name in the iTM.
            **changes: The Unit fields the change sets.

        Raises:
        ------
            ServiceValidationError: The unit does not accept the setpoint.
            HomeAssistantError: The unit is unavailable or the change failed.

        """
        unit = self._read.get(key)
        if unit is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unit_unavailable",
                translation_placeholders={"unit": key},
            )
        change = PendingChange(changes)
        self._pending.setdefault(key, []).append(change)
        self.async_set_updated_data(self._shown())
        try:
            await write(unit.name)
        except SetpointOutOfRange as err:
            self._discard(key, change)
            if key in self._read:
                # The allowed setpoints depend on the operation mode, so the last
                # read may be out of date: show the ones the unit accepts now.
                self._read[key] = dataclasses.replace(
                    self._read[key], setpoint_range=err.allowed
                )
            self.async_set_updated_data(self._shown())
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="setpoint_out_of_range",
                translation_placeholders={
                    "unit": self.display_name(key),
                    "temperature": str(err.value),
                    "min": str(min(err.allowed, default="?")),
                    "max": str(max(err.allowed, default="?")),
                },
            ) from err
        except BaseException as err:
            self._discard(key, change)
            self.async_set_updated_data(self._shown())
            if (error := _service_error(err)) is None:
                raise
            raise error from err
        change.expires = dt_util.utcnow() + PENDING_TIMEOUT
        self.update_interval = CONFIRM_INTERVAL
        self.async_set_updated_data(self._shown())

    def _discard(self, key: str, change: PendingChange) -> None:
        """Stop showing a change, e.g. because the iTM did not accept it."""
        pending = self._pending.get(key, [])
        if change in pending:
            pending.remove(change)
        if not pending:
            self._pending.pop(key, None)


def _service_error(err: BaseException) -> HomeAssistantError | None:
    """Return the error to raise for a failed write, None to raise it as is.

    Args:
    ----
        err: What the write raised.

    Returns:
    -------
        A translated Home Assistant error, or None.

    """
    if isinstance(err, ItmError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"error": str(err)},
        )
    if isinstance(err, (aiohttp.ClientError, TimeoutError)):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        )
    return None
