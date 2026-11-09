"""On the iTM's device: set its clock to Home Assistant's time."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from .entity import ItmHubEntity, add_entities_for_new_items

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from daikin_itm import ItmInfo

    from .coordinator import ItmConfigEntry, ItmSettings, ItmSettingsCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the clock button, once the settings are read.

    Args:
    ----
        _hass: The HomeAssistant instance.
        entry: The config entry.
        async_add_entities: Adds the entities.

    """
    settings = entry.runtime_data.settings
    entry.async_on_unload(
        add_entities_for_new_items(
            settings,
            async_add_entities,
            lambda: ["sync_clock"],
            lambda _: [ItmSyncClockButton(settings)],
        )
    )


class ItmSyncClockButton(ItmHubEntity["ItmSettingsCoordinator"], ButtonEntity):
    """Set the iTM's clock to Home Assistant's local time.

    The iTM keeps its own clock, which drifts; its schedules run on it. It has no
    time zone of its own here: it takes the local time as Home Assistant shows it.
    """

    _attr_translation_key = "sync_clock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ItmSettingsCoordinator) -> None:
        """Initialize the button.

        Args:
        ----
            coordinator: The settings coordinator.

        """
        super().__init__(coordinator, "sync_clock")

    async def async_press(self) -> None:
        """Set the clock to Home Assistant's local time, now."""

        def update(data: ItmSettings, info: ItmInfo) -> ItmSettings:
            return self.coordinator.replace_config(data, info=info)

        await self.coordinator.async_write(
            lambda: self.coordinator.client.set_clock(
                dt_util.now().replace(tzinfo=None, microsecond=0)
            ),
            update,
        )
