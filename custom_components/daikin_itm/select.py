"""What the wall remote controller of each unit may do.

On the iTM's device: the touch panel's buzzer duration and screensaver.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory

from daikin_itm import (
    HardwareSettings,
    RemoteLock,
    ScreensaverMode,
    ScreensaverSettings,
)

from .entity import (
    ItmHubEntity,
    ItmUnitSettingsEntity,
    add_entities_for_listed_units,
    add_entities_for_new_items,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import ItmConfigEntry, ItmSettingsCoordinator

# The buzzer durations in minutes, and how long it sounds otherwise.
BUZZER_DURATIONS = ("1", "3", "5")
BUZZER_CONTINUOUS = "continuous"


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
    """Set up the remote locks of the units that report them, and the touch panel.

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

    def panel() -> list[str]:
        system = settings.data.config.system
        if system is None:
            return []
        found = []
        if system.hardware is not None and (
            system.hardware.buzzer_minutes is not None
            or system.hardware.buzzer_continuous
        ):
            found.append("buzzer_duration")
        if system.screensaver is not None and system.screensaver.mode is not None:
            found.append("screensaver_mode")
        return found

    entry.async_on_unload(
        add_entities_for_new_items(
            settings,
            async_add_entities,
            panel,
            lambda item: [
                ItmBuzzerDurationSelect(settings)
                if item == "buzzer_duration"
                else ItmScreensaverModeSelect(settings)
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


class ItmBuzzerDurationSelect(ItmHubEntity["ItmSettingsCoordinator"], SelectEntity):
    """How long the touch panel's buzzer sounds: minutes, or until stopped."""

    _attr_translation_key = "panel_buzzer_duration"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ItmSettingsCoordinator) -> None:
        """Initialize the select.

        Args:
        ----
            coordinator: The settings coordinator.

        """
        super().__init__(coordinator, "panel_buzzer_duration")
        self._attr_options = [*BUZZER_DURATIONS, BUZZER_CONTINUOUS]

    @property
    def _hardware(self) -> HardwareSettings | None:
        system = self.coordinator.data.config.system
        return system.hardware if system else None

    @property
    def available(self) -> bool:
        """Return if the touch panel settings were read."""
        return super().available and self._hardware is not None

    @property
    def current_option(self) -> str | None:
        """Return the duration."""
        hardware = self._hardware
        if hardware is None:
            return None
        if hardware.buzzer_continuous:
            return BUZZER_CONTINUOUS
        if hardware.buzzer_minutes is None:
            return None
        return str(hardware.buzzer_minutes)

    async def async_select_option(self, option: str) -> None:
        """Change the duration.

        Args:
        ----
            option: Minutes, or continuous.

        """
        client = self.coordinator.client
        await self.coordinator.async_write(
            lambda: (
                client.set_hardware(buzzer_continuous=True)
                if option == BUZZER_CONTINUOUS
                else client.set_hardware(buzzer_minutes=int(option))
            ),
            lambda data, result: self.coordinator.replace_system(data, hardware=result),
        )


class ItmScreensaverModeSelect(ItmHubEntity["ItmSettingsCoordinator"], SelectEntity):
    """What the touch panel does after the screensaver delay."""

    _attr_translation_key = "panel_screensaver_mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ItmSettingsCoordinator) -> None:
        """Initialize the select.

        Args:
        ----
            coordinator: The settings coordinator.

        """
        super().__init__(coordinator, "panel_screensaver_mode")
        self._attr_options = [mode.value for mode in ScreensaverMode]

    @property
    def _screensaver(self) -> ScreensaverSettings | None:
        system = self.coordinator.data.config.system
        return system.screensaver if system else None

    @property
    def available(self) -> bool:
        """Return if the screensaver settings were read."""
        return super().available and self._screensaver is not None

    @property
    def current_option(self) -> str | None:
        """Return the mode."""
        screensaver = self._screensaver
        if screensaver is None or screensaver.mode is None:
            return None
        return screensaver.mode.value

    async def async_select_option(self, option: str) -> None:
        """Change the mode.

        Args:
        ----
            option: A ScreensaverMode value.

        """
        await self.coordinator.async_write(
            lambda: self.coordinator.client.set_screensaver(
                mode=ScreensaverMode(option)
            ),
            lambda data, result: self.coordinator.replace_system(
                data, screensaver=result
            ),
        )
