"""Problems per unit and of the iTM as a whole, for alerts and automations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.util import slugify

from .entity import (
    ItmEntity,
    ItmHubEntity,
    add_entities_for_new_items,
    add_entities_for_new_units,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from daikin_itm import ItmIndicators

    from .coordinator import ItmConfigEntry, ItmCoordinator, ItmSettingsCoordinator

# The main menu bar's indicators: (ItmIndicators field, device class).
INDICATORS = (
    ("energy_saving", None),
    ("error", BinarySensorDeviceClass.PROBLEM),
    ("emergency_stop", BinarySensorDeviceClass.SAFETY),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the problem sensors per unit.

    Args:
    ----
        _hass: The HomeAssistant instance.
        entry: The config entry.
        async_add_entities: Adds the entities.

    """
    coordinator = entry.runtime_data
    entry.async_on_unload(
        add_entities_for_new_units(
            coordinator,
            async_add_entities,
            lambda key: [
                ItmMalfunctionSensor(coordinator, key),
                ItmFilterSensor(coordinator, key),
            ],
        )
    )
    entry.async_on_unload(
        add_entities_for_new_items(
            coordinator,
            async_add_entities,
            lambda: coordinator.analog,
            lambda name: [ItmAnalogAlarmSensor(coordinator, name)],
        )
    )
    settings = coordinator.settings
    entry.async_on_unload(
        add_entities_for_new_items(
            settings,
            async_add_entities,
            lambda: [field for field, _ in INDICATORS],
            lambda field: [
                ItmIndicatorSensor(settings, field, device_class)
                for f, device_class in INDICATORS
                if f == field
            ],
        )
    )


class ItmMalfunctionSensor(ItmEntity, BinarySensorEntity):
    """On while the unit reports an error code."""

    _attr_translation_key = "malfunction"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ItmCoordinator, key: str) -> None:
        """Initialize the sensor.

        Args:
        ----
            coordinator: The coordinator.
            key: The unit's key.

        """
        super().__init__(coordinator, key, "_error")

    @property
    def is_on(self) -> bool | None:
        """Return if the unit reports an error."""
        return self.unit.error_code is not None if self.unit else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the error code."""
        return {"error_code": self.unit.error_code if self.unit else None}


class ItmFilterSensor(ItmEntity, BinarySensorEntity):
    """On while the unit asks for its filter to be cleaned."""

    _attr_translation_key = "filter"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ItmCoordinator, key: str) -> None:
        """Initialize the sensor.

        Args:
        ----
            coordinator: The coordinator.
            key: The unit's key.

        """
        super().__init__(coordinator, key, "_filter")

    @property
    def is_on(self) -> bool | None:
        """Return if the filter sign is shown."""
        return self.unit.filter_sign if self.unit else None


class ItmAnalogAlarmSensor(ItmHubEntity["ItmCoordinator"], BinarySensorEntity):
    """An analog input of the iTM: on while its value is outside its limits."""

    _attr_translation_key = "analog_alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ItmCoordinator, name: str) -> None:
        """Initialize the sensor.

        Args:
        ----
            coordinator: The coordinator.
            name: The point's name in the iTM.

        """
        super().__init__(coordinator, f"analog_{slugify(name)}")
        self._name = name
        self._attr_translation_placeholders = {"name": name}

    @property
    def available(self) -> bool:
        """Return if the point is still in the zone."""
        return super().available and self._name in self.coordinator.analog

    @property
    def is_on(self) -> bool | None:
        """Return if the point is in alarm."""
        point = self.coordinator.analog.get(self._name)
        return point.alarm if point else None


class ItmIndicatorSensor(ItmHubEntity["ItmSettingsCoordinator"], BinarySensorEntity):
    """One of the iTM's main menu bar indicators.

    Read with the settings, so it can lag up to the settings' scan interval.
    """

    def __init__(
        self,
        coordinator: ItmSettingsCoordinator,
        field: str,
        device_class: BinarySensorDeviceClass | None,
    ) -> None:
        """Initialize the sensor.

        Args:
        ----
            coordinator: The settings coordinator.
            field: The ItmIndicators field: energy_saving, error or emergency_stop.
            device_class: The sensor's device class.

        """
        super().__init__(coordinator, field)
        self._field = field
        self._attr_translation_key = field
        self._attr_device_class = device_class

    @property
    def is_on(self) -> bool:
        """Return if the indicator is shown."""
        indicators: ItmIndicators = self.coordinator.data.config.indicators
        on: bool = getattr(indicators, self._field)
        return on
