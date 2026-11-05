"""Base entities: one device per iTM indoor unit, identified by its bus address.

Unit entities follow the units' coordinator (polled every minute), settings entities
the settings coordinator (polled every 15 minutes). Entities of the iTM as a whole
(schedules, changeover groups, the menu bar's indicators) belong to the iTM's device.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ItmCoordinator, ItmSettingsCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from homeassistant.core import CALLBACK_TYPE
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

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
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}{suffix}"
        self._attr_device_info = unit_device_info(coordinator, key)

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


class ItmUnitSettingsEntity(CoordinatorEntity[ItmSettingsCoordinator]):
    """A setting of one indoor unit, as the list view shows it."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: ItmSettingsCoordinator, key: str, suffix: str
    ) -> None:
        """Initialize the entity.

        Args:
        ----
            coordinator: The settings coordinator.
            key: The unit's stable key (its bus address).
            suffix: Added to the unique ID, to tell the unit's entities apart.

        """
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}{suffix}"
        self._attr_device_info = unit_device_info(coordinator.units, key)

    @property
    def unit(self) -> Unit | None:
        """The unit as the list view last showed it."""
        return (
            self.coordinator.data.units.get(self._key)
            if self.coordinator.data
            else None
        )

    @property
    def available(self) -> bool:
        """Return if the unit was read."""
        return super().available and self.unit is not None


class ItmHubEntity[CoordinatorT: ItmCoordinator | ItmSettingsCoordinator](
    CoordinatorEntity[CoordinatorT]
):
    """An entity of the iTM as a whole, on the iTM's device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CoordinatorT, suffix: str) -> None:
        """Initialize the entity.

        Args:
        ----
            coordinator: The coordinator.
            suffix: Added to the unique ID, to tell the iTM's entities apart.

        """
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{suffix}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})


def unit_device_info(coordinator: ItmCoordinator, key: str) -> DeviceInfo:
    """Return the device of a unit.

    Args:
    ----
        coordinator: The units' coordinator.
        key: The unit's key.

    Returns:
    -------
        The device info.

    """
    entry_id = coordinator.config_entry.entry_id
    caps = coordinator.capabilities.get(key)
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{key}")},
        name=coordinator.display_name(key),
        manufacturer="Daikin",
        model=(caps.unit_type if caps else None) or "Indoor unit",
    )
    if coordinator.hub_device_id:
        info["via_device_id"] = coordinator.hub_device_id
    return info


def add_entities_for_new_items(
    coordinator: DataUpdateCoordinator[Any],
    async_add_entities: AddConfigEntryEntitiesCallback,
    items: Callable[[], Iterable[str]],
    factory: Callable[[str], Iterable[Entity]],
) -> CALLBACK_TYPE:
    """Create entities for every item now, and for items that appear later.

    Args:
    ----
        coordinator: The coordinator whose data holds the items.
        async_add_entities: Adds the entities to Home Assistant.
        items: Returns the items' keys or names in the coordinator's data.
        factory: Creates the entities of an item, given its key or name.

    Returns:
    -------
        Removes the listener for new items.

    """
    known: set[str] = set()

    @callback
    def add_new() -> None:
        if coordinator.data is None:  # not read yet
            return
        new = [item for item in items() if item not in known]
        known.update(new)
        if entities := [entity for item in new for entity in factory(item)]:
            async_add_entities(entities)

    add_new()
    return coordinator.async_add_listener(add_new)


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
    return add_entities_for_new_items(
        coordinator, async_add_entities, lambda: coordinator.data, factory
    )


def add_entities_for_listed_units(
    coordinator: ItmSettingsCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> CALLBACK_TYPE:
    """Create settings entities for every unit the list view shows, now and later.

    Args:
    ----
        coordinator: The settings coordinator.
        async_add_entities: Adds the entities to Home Assistant.
        factory: Creates the entities of a unit, given its key; it sees the unit
            as the list view shows it in coordinator.data.

    Returns:
    -------
        Removes the listener for new units.

    """
    return add_entities_for_new_items(
        coordinator, async_add_entities, lambda: coordinator.data.units, factory
    )
