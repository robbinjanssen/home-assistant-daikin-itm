"""Sensors: room temperature and auto mode per unit, and the outdoor temperature.

The room temperature is a sensor so Home Assistant keeps statistics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .auto_mode import Mode
from .const import DOMAIN
from .entity import ItmEntity, add_entities_for_new_units, supports_auto
from .weather import OutdoorCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import ItmConfigEntry, ItmCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of each unit, and the outdoor temperature.

    Args:
    ----
        _hass: The HomeAssistant instance.
        entry: The config entry.
        async_add_entities: Adds the entities.

    """
    coordinator = entry.runtime_data
    if coordinator.outdoor is not None:
        async_add_entities(
            [ItmOutdoorTemperatureSensor(coordinator.outdoor, entry.entry_id)]
        )

    def unit_sensors(key: str) -> list[Entity]:
        sensors: list[Entity] = [ItmRoomTemperatureSensor(coordinator, key)]
        if supports_auto(coordinator.capabilities.get(key)):
            sensors.append(ItmAutoModeSensor(coordinator, key))
        return sensors

    entry.async_on_unload(
        add_entities_for_new_units(coordinator, async_add_entities, unit_sensors)
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


class ItmAutoModeSensor(ItmEntity, SensorEntity):
    """What auto mode chose for the unit, why and when.

    The state is the chosen mode (off while auto mode is off). The attributes tell
    why and when it was chosen, and which change auto mode holds back and why. Each
    decision also shows in the logbook.
    """

    _attr_translation_key = "auto_mode"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: ItmCoordinator, key: str) -> None:
        """Initialize the sensor.

        Args:
        ----
            coordinator: The coordinator.
            key: The unit's key.

        """
        super().__init__(coordinator, key, "_auto_mode")
        self._auto = coordinator.auto_controller(key)
        self._attr_options = ["off", *Mode]

    async def async_added_to_hass(self) -> None:
        """Follow auto mode."""
        await super().async_added_to_hass()
        self.async_on_remove(self._auto.add_listener(self._handle_auto_update))

    @callback
    def _handle_auto_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Return the mode auto mode chose, or off."""
        return str(self._auto.mode) if self._auto.enabled and self._auto.mode else "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why and when auto mode made its last decision, and what it awaits."""
        auto, last = self._auto, self._auto.last
        return {
            "reason": last.reason if last else None,
            "since": last.at.isoformat() if last else None,
            "target": auto.target if auto.enabled else None,
            "temperature": _rounded(last.temperature) if last else None,
            "outdoor_temperature": _rounded(last.outdoor) if last else None,
            "changes_24h": auto.switches_today(dt_util.utcnow()),
            "waiting": auto.waiting,
            "wanted": auto.wanted,
        }


class ItmOutdoorTemperatureSensor(CoordinatorEntity[OutdoorCoordinator], SensorEntity):
    """The outdoor temperature auto mode uses, from Open-Meteo."""

    _attr_has_entity_name = True
    _attr_translation_key = "outdoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_attribution = "Weather data by Open-Meteo.com"

    def __init__(self, coordinator: OutdoorCoordinator, entry_id: str) -> None:
        """Initialize the sensor, on the device of the iTM.

        Args:
        ----
            coordinator: The outdoor temperature coordinator.
            entry_id: The config entry's ID.

        """
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_outdoor_temperature"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    @property
    def native_value(self) -> float | None:
        """Return the outdoor temperature now."""
        return self.coordinator.data.current if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the 24-hour average, which auto mode uses."""
        data = self.coordinator.data
        return {"average_24h": _rounded(data.mean_24h) if data else None}


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
