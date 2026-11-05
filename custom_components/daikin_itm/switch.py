"""Setpoint limits per unit; schedule programs and auto changeover groups."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.util import slugify

from daikin_itm import ChangeoverGroup, ScheduleSummary, TempLimit

from .entity import (
    ItmHubEntity,
    ItmUnitSettingsEntity,
    add_entities_for_listed_units,
    add_entities_for_new_items,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import ItmConfigEntry, ItmSettings, ItmSettingsCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the limit switches, and a switch per program and changeover group.

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
                ItmLimitSwitch(settings, key, field)
                for field in ("cool_limit", "heat_limit")
            ],
        )
    )
    entry.async_on_unload(
        add_entities_for_new_items(
            settings,
            async_add_entities,
            lambda: [p.name for p in settings.data.config.schedules],
            lambda name: [ItmScheduleSwitch(settings, name)],
        )
    )
    entry.async_on_unload(
        add_entities_for_new_items(
            settings,
            async_add_entities,
            lambda: [g.name for g in settings.data.config.changeover_groups],
            lambda name: [ItmChangeoverSwitch(settings, name)],
        )
    )


class ItmLimitSwitch(ItmUnitSettingsEntity, SwitchEntity):
    """Whether a unit's cooling or heating setpoint is limited."""

    def __init__(
        self, coordinator: ItmSettingsCoordinator, key: str, field: str
    ) -> None:
        """Initialize the switch.

        Args:
        ----
            coordinator: The settings coordinator.
            key: The unit's key.
            field: cool_limit or heat_limit.

        """
        super().__init__(coordinator, key, f"_{field}")
        self._field = field
        self._attr_translation_key = field

    @property
    def is_on(self) -> bool | None:
        """Return if the limit is enabled."""
        return getattr(self.unit, self._field) is not None if self.unit else None

    async def _async_set(self, *, on: bool) -> None:
        # Enabling keeps the minimum and maximum the unit has.
        limit = TempLimit(enabled=on, minimum=None, maximum=None)
        await self.coordinator.async_set_unit_settings(
            self._key, **{self._field: limit}
        )

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Enable the limit."""
        await self._async_set(on=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Disable the limit."""
        await self._async_set(on=False)


class ItmScheduleSwitch(ItmHubEntity["ItmSettingsCoordinator"], SwitchEntity):
    """A schedule program of the iTM: whether it runs."""

    _attr_translation_key = "schedule"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ItmSettingsCoordinator, name: str) -> None:
        """Initialize the switch.

        Args:
        ----
            coordinator: The settings coordinator.
            name: The program's name.

        """
        super().__init__(coordinator, f"schedule_{slugify(name)}")
        self._name = name
        self._attr_translation_placeholders = {"name": name}

    @property
    def _program(self) -> ScheduleSummary | None:
        return next(
            (p for p in self.coordinator.data.config.schedules if p.name == self._name),
            None,
        )

    @property
    def available(self) -> bool:
        """Return if the program still exists."""
        return super().available and self._program is not None

    @property
    def is_on(self) -> bool | None:
        """Return if the program is enabled."""
        program = self._program
        return program.enabled if program else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the period the program runs in, if limited."""
        program = self._program
        period = program.period if program else None
        return {"period": "-".join(period) if period else None}

    async def _async_set(self, *, enabled: bool) -> None:
        def update(data: ItmSettings, program: ScheduleSummary) -> ItmSettings:
            schedules = tuple(
                program if p.name == program.name else p for p in data.config.schedules
            )
            return self.coordinator.replace_config(data, schedules=schedules)

        await self.coordinator.async_write(
            lambda: self.coordinator.client.set_schedule_enabled(
                self._name, enabled=enabled
            ),
            update,
        )

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Enable the program."""
        await self._async_set(enabled=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Disable the program."""
        await self._async_set(enabled=False)


class ItmChangeoverSwitch(ItmHubEntity["ItmSettingsCoordinator"], SwitchEntity):
    """An auto changeover group: whether its units switch between heat and cool."""

    _attr_translation_key = "changeover"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ItmSettingsCoordinator, name: str) -> None:
        """Initialize the switch.

        Args:
        ----
            coordinator: The settings coordinator.
            name: The group's name.

        """
        super().__init__(coordinator, f"changeover_{slugify(name)}")
        self._name = name
        self._attr_translation_placeholders = {"name": name}

    @property
    def _group(self) -> ChangeoverGroup | None:
        return next(
            (
                g
                for g in self.coordinator.data.config.changeover_groups
                if g.name == self._name
            ),
            None,
        )

    @property
    def available(self) -> bool:
        """Return if the group still exists."""
        return super().available and self._group is not None

    @property
    def is_on(self) -> bool | None:
        """Return if the group is enabled."""
        group = self._group
        return group.enabled if group else None

    async def _async_set(self, *, enabled: bool) -> None:
        def update(data: ItmSettings, group: ChangeoverGroup) -> ItmSettings:
            # Only take the enabled state: the groups as read hold no details.
            groups = tuple(
                dataclasses.replace(g, enabled=group.enabled)
                if g.name == group.name
                else g
                for g in data.config.changeover_groups
            )
            return self.coordinator.replace_config(data, changeover_groups=groups)

        await self.coordinator.async_write(
            lambda: self.coordinator.client.set_changeover_enabled(self._name, enabled),
            update,
        )

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Enable the group."""
        await self._async_set(enabled=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Disable the group."""
        await self._async_set(enabled=False)
