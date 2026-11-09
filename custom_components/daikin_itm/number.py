"""The units' setpoint limits; on the iTM's device its settings.

The overtime timer duration, the setback offsets and recovery temperatures, and the
touch panel's backlight and volumes.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime

from daikin_itm import HardwareSettings, SetbackSettings, TempLimit

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

# The SetbackSettings fields: the offsets of setback high and low, and the recovery
# temperatures. All are differences in °C, so they have no temperature device class
# (which would convert them as absolute temperatures).
SETBACK = (
    "high_up",
    "high_back",
    "low_up",
    "low_back",
    "recovery_cool",
    "recovery_heat",
)


@dataclasses.dataclass(frozen=True)
class PanelDescription:
    """A touch panel setting: a HardwareSettings field and its range."""

    field: str
    minimum: int
    maximum: int


PANEL = (
    PanelDescription("backlight", 1, 8),
    PanelDescription("buzzer_volume", 0, 5),
    PanelDescription("key_click_volume", 0, 5),
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

    def setback() -> list[str]:
        values = settings.data.config.setback
        return [key for key in SETBACK if getattr(values, f"{key}_options")]

    entry.async_on_unload(
        add_entities_for_new_items(
            settings,
            async_add_entities,
            setback,
            lambda key: [ItmSetbackNumber(settings, key)],
        )
    )

    def panel() -> list[str]:
        system = settings.data.config.system
        hardware = system.hardware if system else None
        if hardware is None:
            return []
        return [d.field for d in PANEL if getattr(hardware, d.field) is not None]

    entry.async_on_unload(
        add_entities_for_new_items(
            settings,
            async_add_entities,
            panel,
            lambda field: [
                ItmPanelNumber(settings, d) for d in PANEL if d.field == field
            ],
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


class ItmSetbackNumber(ItmHubEntity["ItmSettingsCoordinator"], NumberEntity):
    """A setback offset or recovery temperature, within what the iTM offers."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1

    def __init__(self, coordinator: ItmSettingsCoordinator, field: str) -> None:
        """Initialize the number.

        Args:
        ----
            coordinator: The settings coordinator.
            field: The SetbackSettings field.

        """
        super().__init__(coordinator, f"setback_{field}")
        self._field = field
        self._attr_translation_key = f"setback_{field}"

    @property
    def _options(self) -> tuple[int, ...]:
        """The values the iTM offers."""
        options: tuple[int, ...] = getattr(
            self.coordinator.data.config.setback, f"{self._field}_options"
        )
        return options

    @property
    def available(self) -> bool:
        """Return if the iTM showed the setting."""
        return super().available and bool(self._options)

    @property
    def native_min_value(self) -> float:
        """Return the lowest value the iTM offers."""
        return min(self._options, default=0)

    @property
    def native_max_value(self) -> float:
        """Return the highest value the iTM offers."""
        return max(self._options, default=0)

    @property
    def native_value(self) -> int | None:
        """Return the setting."""
        value: int | None = getattr(self.coordinator.data.config.setback, self._field)
        return value

    async def async_set_native_value(self, value: float) -> None:
        """Change the setting.

        Args:
        ----
            value: One of the values the iTM offers.

        """

        def update(data: ItmSettings, result: SetbackSettings) -> ItmSettings:
            return self.coordinator.replace_config(data, setback=result)

        await self.coordinator.async_write(
            lambda: self.coordinator.client.set_setback(**{self._field: int(value)}),
            update,
        )


class ItmPanelNumber(ItmHubEntity["ItmSettingsCoordinator"], NumberEntity):
    """The touch panel's backlight level, buzzer volume or key click volume."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_step = 1

    def __init__(
        self, coordinator: ItmSettingsCoordinator, description: PanelDescription
    ) -> None:
        """Initialize the number.

        Args:
        ----
            coordinator: The settings coordinator.
            description: The setting and its range.

        """
        super().__init__(coordinator, f"panel_{description.field}")
        self._field = description.field
        self._attr_translation_key = f"panel_{description.field}"
        self._attr_native_min_value = description.minimum
        self._attr_native_max_value = description.maximum

    @property
    def _hardware(self) -> HardwareSettings | None:
        system = self.coordinator.data.config.system
        return system.hardware if system else None

    @property
    def available(self) -> bool:
        """Return if the touch panel settings were read."""
        return super().available and self._hardware is not None

    @property
    def native_value(self) -> int | None:
        """Return the setting."""
        hardware = self._hardware
        value: int | None = getattr(hardware, self._field) if hardware else None
        return value

    async def async_set_native_value(self, value: float) -> None:
        """Change the setting.

        Args:
        ----
            value: The new level or volume.

        """
        changes: dict[str, Any] = {self._field: int(value)}
        await self.coordinator.async_write(
            lambda: self.coordinator.client.set_hardware(**changes),
            lambda data, result: self.coordinator.replace_system(data, hardware=result),
        )
