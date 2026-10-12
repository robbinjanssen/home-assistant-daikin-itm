"""Room temperature per unit, as a sensor so Home Assistant keeps statistics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature

from .entity import ItmEntity, add_entities_for_new_units

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import ItmConfigEntry, ItmCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a room temperature sensor per unit.

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
            lambda key: [ItmRoomTemperatureSensor(coordinator, key)],
        )
    )


class ItmRoomTemperatureSensor(ItmEntity, SensorEntity):
    """The temperature measured at the unit's air intake."""

    _attr_translation_key = "room_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: ItmCoordinator, key: str) -> None:
        """Initialize the sensor.

        Args:
        ----
            coordinator: The coordinator.
            key: The unit's key.

        """
        super().__init__(coordinator, key, "_room_temperature")

    @property
    def native_value(self) -> float | None:
        """Return the room temperature.

        Rough while the unit is off, in thermo-off or defrosting.
        """
        return self.unit.room_temperature if self.unit else None
