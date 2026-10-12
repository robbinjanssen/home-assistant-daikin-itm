"""Malfunction and filter warning per unit, for alerts and automations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory

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
