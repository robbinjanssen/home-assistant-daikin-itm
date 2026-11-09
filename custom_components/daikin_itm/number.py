"""The units' setpoint limits and the iTM's overtime timer duration."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime

from daikin_itm import TempLimit

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

# The range the iTM offers for setpoints; it refuses limits outside a unit's range.
LIMIT_MIN = 16
LIMIT_MAX = 32
# The overtime timer durations the iTM offers.
OVERTIME_MIN = 30
OVERTIME_MAX = 180
OVERTIME_STEP = 30


@dataclasses.dataclass(frozen=True)
class LimitDescription:
    """One end of a unit's cooling or heating setpoint limit."""

    # The Unit and set_unit_settings field: cool_limit or heat_limit.
    field: str
    # 0 for the minimum, 1 for the maximum.
    end: int
    translation_key: str


LIMITS = (
    LimitDescription("cool_limit", 0, "cool_limit_min"),
    LimitDescription("cool_limit", 1, "cool_limit_max"),
    LimitDescription("heat_limit", 0, "heat_limit_min"),
    LimitDescription("heat_limit", 1, "heat_limit_max"),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the limits per unit and the overtime duration.

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
            lambda key: [ItmLimitNumber(settings, key, limit) for limit in LIMITS],
        )
    )
    entry.async_on_unload(
        add_entities_for_new_items(
            settings,
            async_add_entities,
            lambda: (
                ["overtime"]
                if settings.data.config.overtime_minutes is not None
                else []
            ),
            lambda _: [ItmOvertimeNumber(settings)],
        )
    )


class ItmLimitNumber(ItmUnitSettingsEntity, NumberEntity):
    """The lowest or highest setpoint allowed while cooling or heating.

    Only available while the limit is enabled (see the limit switches): the list view
    shows no values for a disabled limit.
    """

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = LIMIT_MIN
    _attr_native_max_value = LIMIT_MAX
    _attr_native_step = 1

    def __init__(
        self, coordinator: ItmSettingsCoordinator, key: str, limit: LimitDescription
    ) -> None:
        """Initialize the number.

        Args:
        ----
            coordinator: The settings coordinator.
            key: The unit's key.
            limit: Which end of which limit.

        """
        super().__init__(coordinator, key, f"_{limit.translation_key}")
        self._limit = limit
        self._attr_translation_key = limit.translation_key

    @property
    def _values(self) -> tuple[float, float] | None:
        """The limit's minimum and maximum, None while it is disabled."""
        if self.unit is None:
            return None
        values: tuple[float, float] | None = getattr(self.unit, self._limit.field)
        return values

    @property
    def available(self) -> bool:
        """Return if the limit is enabled."""
        return super().available and self._values is not None

    @property
    def native_value(self) -> float | None:
        """Return the limit."""
        values = self._values
        return values[self._limit.end] if values else None

    async def async_set_native_value(self, value: float) -> None:
        """Change the limit; the other end stays as it is.

        Args:
        ----
            value: The new minimum or maximum.

        """
        limit = (
            TempLimit(enabled=True, minimum=value, maximum=None)
            if self._limit.end == 0
            else TempLimit(enabled=True, minimum=None, maximum=value)
        )
        await self.coordinator.async_set_unit_settings(
            self._key, **{self._limit.field: limit}
        )


class ItmOvertimeNumber(ItmHubEntity["ItmSettingsCoordinator"], NumberEntity):
    """How long the overtime timer keeps a unit running past its schedule."""

    _attr_translation_key = "overtime_duration"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = OVERTIME_MIN
    _attr_native_max_value = OVERTIME_MAX
    _attr_native_step = OVERTIME_STEP

    def __init__(self, coordinator: ItmSettingsCoordinator) -> None:
        """Initialize the number.

        Args:
        ----
            coordinator: The settings coordinator.

        """
        super().__init__(coordinator, "overtime_duration")

    @property
    def native_value(self) -> int | None:
        """Return the duration in minutes."""
        return self.coordinator.data.config.overtime_minutes

    async def async_set_native_value(self, value: float) -> None:
        """Change the duration.

        Args:
        ----
            value: 30 to 180 minutes, in steps of 30.

        """
        await self.coordinator.async_write(
            lambda: self.coordinator.client.set_overtime_duration(int(value)),
            lambda data, minutes: self.coordinator.replace_config(
                data, overtime_minutes=minutes
            ),
        )
