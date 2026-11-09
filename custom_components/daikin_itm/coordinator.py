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
    AnalogPoint,
    Capabilities,
    ItmClient,
    ItmConfig,
    ItmError,
    LoginRejected,
    SetpointOutOfRange,
    TempLimit,
    Unit,
    UnitSettings,
)

from .api import unique_id_for_host
from .auto_mode import AutoController
from .const import (
    CONFIRM_INTERVAL,
    DOMAIN,
    LOGGER,
    LOGIN_REJECTED_ISSUE_AFTER,
    MAX_TOLERATED_LOGIN_REJECTIONS,
    PENDING_TIMEOUT,
    SCAN_INTERVAL,
    SETTINGS_SCAN_INTERVAL,
    SYSTEM_SCREENS,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

    from .weather import OutdoorCoordinator

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
        # Analog inputs in the zone, by name: they have no bus address.
        self.analog: dict[str, AnalogPoint] = {}
        self.settings = ItmSettingsCoordinator(hass, entry, self)
        # The device of the iTM itself, set up before the entities.
        self.hub_device_id: str | None = None
        self.outdoor: OutdoorCoordinator | None = None
        # When the units were last read; refused logins keep showing older data.
        self.read_at: datetime | None = None
        self._auto: dict[str, AutoController] = {}  # by key

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
            points = await self.client.fetch_points()
            units = {n: p for n, p in points.items() if isinstance(p, Unit)}
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
        self.read_at = dt_util.utcnow()
        self._read = self.by_key(units)
        self.analog = {n: p for n, p in points.items() if isinstance(p, AnalogPoint)}
        self._drop_settled_changes()
        return self._shown()

    def has_pending(self, key: str) -> bool:
        """Return if a change to a unit is being written or not yet shown by a poll.

        Args:
        ----
            key: The unit's key.

        Returns:
        -------
            True while the unit as read may not show a change Home Assistant made.

        """
        return bool(self._pending.get(key))

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

    def by_key[T](self, by_name: dict[str, T]) -> dict[str, T]:
        """Key what the iTM returns by unit name by the units' stable keys.

        Args:
        ----
            by_name: Anything by unit name.

        Returns:
        -------
            The same by key, without the names that are no known unit.

        """
        return {
            self._key_by_name[name]: value
            for name, value in by_name.items()
            if name in self._key_by_name
        }

    def name_of(self, key: str) -> str:
        """Return a unit's current name in the iTM.

        Args:
        ----
            key: The unit's key.

        Returns:
        -------
            The unit's name.

        Raises:
        ------
            HomeAssistantError: The unit was not read.

        """
        unit = self._read.get(key)
        if unit is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unit_unavailable",
                translation_placeholders={"unit": key},
            )
        return unit.name

    def auto_controller(self, key: str) -> AutoController:
        """Return the auto mode of a unit, shared by its entities.

        Args:
        ----
            key: The unit's key.

        Returns:
        -------
            The unit's auto mode.

        """
        return self._auto.setdefault(key, AutoController())

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
        name = self.name_of(key)
        change = PendingChange(changes)
        self._pending.setdefault(key, []).append(change)
        self.async_set_updated_data(self._shown())
        try:
            await write(name)
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


@dataclasses.dataclass(frozen=True)
class ItmSettings:
    """What the iTM's menu and list view show, read far less often than the units."""

    config: ItmConfig
    # The units as the list view shows them, by key: with their wall remote locks,
    # setpoint limits and airflow direction, which the icon view lacks.
    units: dict[str, Unit]


class ItmSettingsCoordinator(DataUpdateCoordinator[ItmSettings]):
    """Read the settings: the menu's configuration and the units' list view.

    The menu bar's indicators, the version and clock, the schedule programs, the auto
    changeover groups, setback, the overtime duration and a few system screens (the
    touch panel, the confirm dialogs and remote management) come from one session
    (fetch_config), the units' locks, limits and airflow direction from a second
    (the list view, one request).
    """

    config_entry: ItmConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ItmConfigEntry, units: ItmCoordinator
    ) -> None:
        """Initialize the coordinator.

        Args:
        ----
            hass: The HomeAssistant instance.
            entry: The config entry.
            units: The coordinator of the units, which maps names to keys.

        """
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_settings",
            update_interval=SETTINGS_SCAN_INTERVAL,
        )
        self.units = units
        self.client = units.client

    async def _async_update_data(self) -> ItmSettings:
        """Read the settings, in two sessions.

        Returns
        -------
            The settings.

        Raises
        ------
            UpdateFailed: The iTM could not be read.

        """
        try:
            config = await self.client.fetch_config(system=SYSTEM_SCREENS)
            listed = await self.client.fetch_units(view="list")
        except LoginRejected as err:
            # Usually someone uses the web UI; the units' coordinator tells when
            # logins keep being refused.
            if self.data is not None and self.units.last_update_success:
                return self.data
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="login_rejected"
            ) from err
        except (ItmError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return ItmSettings(config, self.units.by_key(listed))

    async def async_write[T](
        self,
        write: Callable[[], Awaitable[T | None]],
        update: Callable[[ItmSettings, T], ItmSettings],
    ) -> None:
        """Change a setting and show it as the iTM reads it back.

        Settings writes are confirmed by the library (a read in a fresh session), so
        unlike unit writes they are not shown before the iTM accepted them.

        Args:
        ----
            write: Sends the change and returns the setting as read back.
            update: Puts the setting as read back into the settings.

        Raises:
        ------
            HomeAssistantError: The change failed.

        """
        try:
            result = await write()
        except BaseException as err:
            if (error := _service_error(err)) is None:
                raise
            raise error from err
        if result is not None and self.data is not None:
            self.async_set_updated_data(update(self.data, result))

    async def async_set_unit_settings(self, key: str, **changes: Any) -> None:
        """Change a unit's locks, limits or airflow direction.

        Args:
        ----
            key: The unit's key.
            **changes: The arguments of ItmClient.set_unit_settings.

        """
        name = self.units.name_of(key)

        def update(data: ItmSettings, result: UnitSettings) -> ItmSettings:
            unit = data.units.get(key)
            if unit is None:
                return data
            unit = dataclasses.replace(
                unit,
                remote_locks=result.remote_locks,
                cool_limit=_limit(result.cool_limit),
                heat_limit=_limit(result.heat_limit),
                airflow_direction=result.airflow_direction,
            )
            return dataclasses.replace(data, units={**data.units, key: unit})

        await self.async_write(
            lambda: self.client.set_unit_settings(name, **changes), update
        )

    def replace_config(self, data: ItmSettings, **changes: Any) -> ItmSettings:
        """Return the settings with some of the configuration replaced.

        Args:
        ----
            data: The settings.
            **changes: The ItmConfig fields to replace.

        Returns:
        -------
            The new settings.

        """
        return dataclasses.replace(
            data, config=dataclasses.replace(data.config, **changes)
        )

    def replace_system(self, data: ItmSettings, **changes: Any) -> ItmSettings:
        """Return the settings with some of the system settings replaced.

        Args:
        ----
            data: The settings.
            **changes: The SystemSettings fields to replace.

        Returns:
        -------
            The new settings.

        """
        system = data.config.system
        if system is None:
            return data
        return self.replace_config(data, system=dataclasses.replace(system, **changes))


def _limit(limit: TempLimit) -> tuple[float, float] | None:
    """Return a setpoint limit as the list view shows it: None while disabled."""
    if limit.enabled and limit.minimum is not None and limit.maximum is not None:
        return (limit.minimum, limit.maximum)
    return None


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
