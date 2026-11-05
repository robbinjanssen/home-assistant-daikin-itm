"""What the wall remote controller of each unit may do."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity

from daikin_itm import RemoteLock

from .entity import ItmUnitSettingsEntity, add_entities_for_listed_units

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import ItmConfigEntry, ItmSettingsCoordinator


@dataclasses.dataclass(frozen=True)
class RemoteLockDescription:
    """One of the wall remote's locks."""

    # The RemoteLocks field, also the translation key and unique ID suffix.
    field: str
    # The set_unit_settings argument.
    argument: str
    options: tuple[RemoteLock, ...]


LOCKS = (
    RemoteLockDescription(
        "start_stop",
        "start_stop_lock",
        (RemoteLock.ALLOWED, RemoteLock.STOP_ONLY, RemoteLock.FORBIDDEN),
    ),
    RemoteLockDescription(
        "mode", "mode_lock", (RemoteLock.ALLOWED, RemoteLock.FORBIDDEN)
    ),
    RemoteLockDescription(
        "setpoint", "setpoint_lock", (RemoteLock.ALLOWED, RemoteLock.FORBIDDEN)
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the remote locks of the units that report them.

    Args:
    ----
        _hass: The HomeAssistant instance.
        entry: The config entry.
        async_add_entities: Adds the entities.

    """
    settings = entry.runtime_data.settings
    entry.async_on_unload(
        add_entities_for_listed_units(
            settings,
            async_add_entities,
            lambda key: [
                ItmRemoteLockSelect(settings, key, lock)
                for lock in LOCKS
                if settings.data.units[key].remote_locks is not None
            ],
        )
    )


class ItmRemoteLockSelect(ItmUnitSettingsEntity, SelectEntity):
    """Whether the wall remote may start/stop, change the mode or the setpoint."""

    def __init__(
        self,
        coordinator: ItmSettingsCoordinator,
        key: str,
        lock: RemoteLockDescription,
    ) -> None:
        """Initialize the select.

        Args:
        ----
            coordinator: The settings coordinator.
            key: The unit's key.
            lock: Which lock.

        """
        super().__init__(coordinator, key, f"_{lock.field}_lock")
        self._lock = lock
        self._attr_translation_key = f"{lock.field}_lock"
        self._attr_options = [option.value for option in lock.options]

    @property
    def current_option(self) -> str | None:
        """Return the lock, None when unknown (e.g. a label in another language)."""
        locks = self.unit.remote_locks if self.unit else None
        if locks is None:
            return None
        value: RemoteLock = getattr(locks, self._lock.field)
        return value.value if value in self._lock.options else None

    async def async_select_option(self, option: str) -> None:
        """Change the lock.

        Args:
        ----
            option: allowed, stop_only (start/stop only) or forbidden.

        """
        await self.coordinator.async_set_unit_settings(
            self._key, **{self._lock.argument: RemoteLock(option)}
        )
