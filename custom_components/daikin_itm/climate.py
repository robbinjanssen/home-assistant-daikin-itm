"""One climate entity per iTM indoor unit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError

from daikin_itm import FAN_SPEEDS

from .const import DOMAIN
from .entity import ItmEntity, add_entities_for_new_units

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import ItmConfigEntry

MODE_TO_HVAC = {
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "fan": HVACMode.FAN_ONLY,
    "dry": HVACMode.DRY,
    "auto": HVACMode.HEAT_COOL,
}
HVAC_TO_MODE = {v: k for k, v in MODE_TO_HVAC.items()}

# Used until the unit reports its allowed setpoints.
DEFAULT_MIN_TEMP = 16
DEFAULT_MAX_TEMP = 32


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ItmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a climate entity per unit.

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
            lambda key: [ItmClimate(coordinator, key)],
        )
    )


class ItmClimate(ItmEntity, ClimateEntity):
    """An indoor unit.

    Home Assistant reads the capability properties (hvac_modes, min/max temp, fan
    modes) even while the entity is unavailable, so none of them may assume that
    self.unit is set.
    """

    _attr_name = None  # the device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return the features of the unit."""
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self.caps and self.caps.fan_speed:
            features |= ClimateEntityFeature.FAN_MODE
        return features

    @property
    def _current_mode(self) -> HVACMode | None:
        """The unit's operation mode, None when unknown (e.g. an unknown label)."""
        if self.unit is None or self.unit.mode is None:
            return None
        return MODE_TO_HVAC.get(self.unit.mode)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the operation mode, or off."""
        if self.unit is None:
            return None
        return self._current_mode if self.unit.running else HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return off plus the modes the unit offers."""
        modes = (
            [MODE_TO_HVAC[m] for m in self.caps.modes if m in MODE_TO_HVAC]
            if self.caps
            else []
        )
        if self._current_mode is not None and self._current_mode not in modes:
            modes.append(self._current_mode)
        return [HVACMode.OFF, *modes]

    @property
    def fan_mode(self) -> str | None:
        """Return the fan speed."""
        return self.unit.fan_speed if self.unit else None

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the fan speeds, if the unit's fan speed can be set."""
        return list(FAN_SPEEDS) if self.caps and self.caps.fan_speed else None

    @property
    def current_temperature(self) -> float | None:
        """Return the room temperature."""
        return self.unit.room_temperature if self.unit else None

    @property
    def target_temperature(self) -> float | None:
        """Return the setpoint, None while the unit is stopped.

        The iTM ignores setpoint changes of a stopped unit, so it is not offered then.
        """
        return self.unit.setpoint if self.unit and self.unit.running else None

    @property
    def min_temp(self) -> float:
        """Return the lowest allowed setpoint."""
        if self.unit and self.unit.setpoint_range:
            return self.unit.setpoint_range[0]
        return DEFAULT_MIN_TEMP

    @property
    def max_temp(self) -> float:
        """Return the highest allowed setpoint."""
        if self.unit and self.unit.setpoint_range:
            return self.unit.setpoint_range[-1]
        return DEFAULT_MAX_TEMP

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the unit's name and state as the iTM shows them."""
        unit = self.unit
        if unit is None:
            return {}
        return {
            "itm_name": unit.name,
            "itm_power": unit.power,
            "itm_mode": unit.mode_label,
            "setback": unit.setback,
        }

    def _ensure_running(self) -> None:
        if self.unit is not None and self.unit.running is False:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unit_stopped",
                translation_placeholders={"unit": self.display_name},
            )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Change the setpoint, and the operation mode if given.

        Args:
        ----
            **kwargs: The temperature and optionally the operation mode.

        """
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if hvac_mode == HVACMode.OFF:
            # The iTM ignores setpoints of stopped units: set the temperature first.
            if temperature is not None:
                await self._async_set_setpoint(temperature)
            await self.async_turn_off()
            return
        if hvac_mode is not None:
            await self.async_set_hvac_mode(hvac_mode)
        if temperature is not None:
            # The library waits until a start above reached the unit.
            await self._async_set_setpoint(temperature)

    async def _async_set_setpoint(self, temperature: float) -> None:
        self._ensure_running()
        value = round(temperature)
        await self.coordinator.async_write(
            self._key,
            lambda name: self.coordinator.client.set_setpoint(
                name, value, confirm=False
            ),
            setpoint=value,
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Start the unit in an operation mode, or stop it.

        Args:
        ----
            hvac_mode: The operation mode, or off.

        Raises:
        ------
            ServiceValidationError: The unit does not offer the operation mode.

        """
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        mode = HVAC_TO_MODE.get(hvac_mode)
        if mode is None or hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="mode_not_supported",
                translation_placeholders={"unit": self.display_name, "mode": hvac_mode},
            )
        unit = self.unit
        label = self.caps.mode_labels.get(mode) if self.caps else None
        # Ask for the full state: the library compares it with the unit as it reads
        # it while writing (it may have been stopped since the last poll), and starts
        # and/or switches mode in one settings dialog write.
        await self.coordinator.async_write(
            self._key,
            lambda name: self.coordinator.client.apply(
                name, power=True, mode=mode, confirm=False
            ),
            running=True,
            mode=mode,
            mode_label=label or (unit.mode_label if unit else None),
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Change the fan speed.

        Args:
        ----
            fan_mode: low, medium, high or auto.

        """
        await self.coordinator.async_write(
            self._key,
            lambda name: self.coordinator.client.set_fan_speed(
                name, fan_mode, confirm=False
            ),
            fan_speed=fan_mode,
        )

    async def async_turn_on(self) -> None:
        """Start the unit in its last operation mode."""
        await self.coordinator.async_write(
            self._key,
            lambda name: self.coordinator.client.set_power(
                name, on=True, confirm=False
            ),
            running=True,
        )

    async def async_turn_off(self) -> None:
        """Stop the unit."""
        await self.coordinator.async_write(
            self._key,
            lambda name: self.coordinator.client.set_power(
                name, on=False, confirm=False
            ),
            running=False,
        )
