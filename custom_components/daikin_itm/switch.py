"""Per unit: whether auto mode may ventilate once the room is warm or cool enough."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from .entity import ItmEntity, add_entities_for_new_units, supports_auto

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import ItmConfigEntry, ItmCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch for each unit that auto mode can ventilate with.

    Args:
    ----
        _hass: The HomeAssistant instance.
        entry: The config entry.
        async_add_entities: Adds the entities.

    """
    coordinator = entry.runtime_data

    def switches(key: str) -> list[ItmAutoVentilateSwitch]:
        caps = coordinator.capabilities.get(key)
        if supports_auto(caps) and caps is not None and "fan" in caps.modes:
            return [ItmAutoVentilateSwitch(coordinator, key)]
        return []

    entry.async_on_unload(
        add_entities_for_new_units(coordinator, async_add_entities, switches)
    )


class ItmAutoVentilateSwitch(ItmEntity, SwitchEntity):
    """Let auto mode switch to fan instead of heating or cooling past the target.

    Saves energy, at the cost of a little more temperature swing and more mode
    changes. The climate entity stores the setting with the rest of auto mode.
    """

    _attr_translation_key = "auto_ventilate"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ItmCoordinator, key: str) -> None:
        """Initialize the switch.

        Args:
        ----
            coordinator: The coordinator.
            key: The unit's key.

        """
        super().__init__(coordinator, key, "_auto_ventilate")
        self._auto = coordinator.auto_controller(key)

    async def async_added_to_hass(self) -> None:
        """Follow auto mode, whose state the climate entity restores."""
        await super().async_added_to_hass()
        self.async_on_remove(self._auto.add_listener(self._handle_auto_update))

    @callback
    def _handle_auto_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return if auto mode may ventilate."""
        return self._auto.ventilate

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Let auto mode ventilate."""
        self._set(ventilate=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Let auto mode heat or cool only; it stops ventilating right away."""
        self._set(ventilate=False)

    def _set(self, *, ventilate: bool) -> None:
        self._auto.ventilate = ventilate
        self._auto.notify()
        # Let the unit's climate entity act on it now rather than at the next poll.
        self._auto.request_decision()
