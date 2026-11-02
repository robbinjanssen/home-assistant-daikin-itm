"""Base entity: one device per iTM indoor unit, identified by its bus address."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ItmCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from homeassistant.core import CALLBACK_TYPE
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from daikin_itm import Capabilities, Unit


class ItmEntity(CoordinatorEntity[ItmCoordinator]):
    """An entity of one indoor unit."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ItmCoordinator, key: str, suffix: str = "") -> None:
        """Initialize the entity.

        Args:
        ----
            coordinator: The coordinator.
            key: The unit's stable key (its bus address).
            suffix: Added to the unique ID, to tell the unit's entities apart.

        """
        super().__init__(coordinator)
        self._key = key
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}{suffix}"
        caps = coordinator.capabilities.get(key)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{key}")},
            name=self.display_name,
            manufacturer="Daikin",
            model=(caps.unit_type if caps else None) or "Indoor unit",
        )
        if coordinator.hub_device_id:
            self._attr_device_info["via_device_id"] = coordinator.hub_device_id

    @property
    def unit(self) -> Unit | None:
        """The unit as last read, None when it is no longer in the zone."""
        return self.coordinator.data.get(self._key)

    @property
    def caps(self) -> Capabilities | None:
        """What the unit offers, None when unknown."""
        return self.coordinator.capabilities.get(self._key)

    @property
    def display_name(self) -> str:
        """The unit's name for people, see ItmCoordinator.display_name."""
        return self.coordinator.display_name(self._key)

    @property
    def available(self) -> bool:
        """Return if the unit was read."""
        return super().available and self.unit is not None


def rounded(value: float | None) -> float | None:
    """Return a temperature rounded to a tenth of a degree, for display.

    Args:
    ----
        value: The temperature, or None.

    Returns:
    -------
        The rounded temperature, or None.

    """
    return round(value, 1) if value is not None else None


def supports_auto(caps: Capabilities | None) -> bool:
    """Return if auto mode can control a unit: it must offer heating and cooling.

    Args:
    ----
        caps: What the unit offers.

    Returns:
    -------
        True if the unit offers heat and cool.

    """
    return caps is not None and {"heat", "cool"} <= set(caps.modes)


def add_entities_for_new_units(
    coordinator: ItmCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> CALLBACK_TYPE:
    """Create entities for every unit now, and for units that appear later.

    Args:
    ----
        coordinator: The coordinator.
        async_add_entities: Adds the entities to Home Assistant.
        factory: Creates the entities of a unit, given its key.

    Returns:
    -------
        Removes the listener for new units.

    """
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = [key for key in coordinator.data if key not in known]
        known.update(new)
        async_add_entities([entity for key in new for entity in factory(key)])

    add_new()
    return coordinator.async_add_listener(add_new)
