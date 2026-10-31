"""One climate entity per iTM indoor unit, with auto mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity
from homeassistant.util import dt as dt_util

from daikin_itm import FAN_SPEEDS

from .auto_mode import Mode, Reason
from .const import AUTO_WRITE_GRACE, DOMAIN, EVENT_AUTO_MODE, LOGGER
from .entity import ItmEntity, add_entities_for_new_units, supports_auto

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .auto_mode import Decision
    from .coordinator import ItmConfigEntry, ItmCoordinator

MODE_TO_HVAC = {
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "fan": HVACMode.FAN_ONLY,
    "dry": HVACMode.DRY,
    "auto": HVACMode.HEAT_COOL,
}
HVAC_TO_MODE = {v: k for k, v in MODE_TO_HVAC.items()}
MODE_TO_ACTION = {
    "heat": HVACAction.HEATING,
    "cool": HVACAction.COOLING,
    "fan": HVACAction.FAN,
    "dry": HVACAction.DRYING,
}

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


class ItmClimate(ItmEntity, ClimateEntity, RestoreEntity):
    """An indoor unit.

    Home Assistant reads the capability properties (hvac_modes, min/max temp, fan
    modes) even while the entity is unavailable, so none of them may assume that
    self.unit is set.

    In auto mode (HVACMode.AUTO, for units that offer heating and cooling) the
    entity's AutoController chooses heat, cool or fan on every poll, and the target
    temperature is the controller's. Auto mode stops when another mode is chosen,
    or when the unit is changed elsewhere (remote controller, iTM). A setpoint
    changed elsewhere becomes the new target.
    """

    _attr_name = None  # the device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1

    def __init__(self, coordinator: ItmCoordinator, key: str) -> None:
        """Initialize the entity.

        Args:
        ----
            coordinator: The coordinator.
            key: The unit's key.

        """
        super().__init__(coordinator, key)
        self._auto = coordinator.auto_controller(key)
        self._auto_busy = False  # writing for auto mode
        self._auto_written: datetime | None = None  # last write for auto mode
        # Whether the unit's setpoint was the target since the last start or restart.
        self._setpoint_synced = False

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
        """Return the operation mode, auto or off."""
        if self.unit is None:
            return None
        if self._auto.enabled:
            return HVACMode.AUTO
        return self._current_mode if self.unit.running else HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return off plus the modes the unit offers, and auto mode."""
        modes = (
            [MODE_TO_HVAC[m] for m in self.caps.modes if m in MODE_TO_HVAC]
            if self.caps
            else []
        )
        if self._current_mode is not None and self._current_mode not in modes:
            modes.append(self._current_mode)
        if supports_auto(self.caps):
            modes.append(HVACMode.AUTO)
        return [HVACMode.OFF, *modes]

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the unit's operation mode as an action, e.g. what auto mode chose.

        The iTM does not tell whether the unit is in thermo-off, so a heating unit
        is reported as heating also once the room is warm enough.
        """
        unit = self.unit
        if unit is None or unit.running is None:
            return None
        if not unit.running:
            return HVACAction.OFF
        return MODE_TO_ACTION.get(unit.mode) if unit.mode else None

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
        """Return the setpoint, None while the unit is stopped; in auto mode the target.

        The iTM ignores setpoint changes of a stopped unit, so it is not offered then.
        """
        if self._auto.enabled:
            return self._auto.target
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
        if hvac_mode == HVACMode.AUTO and not self._auto.enabled:
            await self._async_start_auto(temperature)
            return
        if hvac_mode in (HVACMode.AUTO, None) and self._auto.enabled:
            if temperature is not None:
                await self._async_set_auto_target(temperature)
            return
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
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="mode_not_supported",
                translation_placeholders={"unit": self.display_name, "mode": hvac_mode},
            )
        if hvac_mode == HVACMode.AUTO:
            if not self._auto.enabled:
                await self._async_start_auto(None)
            return
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        self._stop_auto()
        await self._async_apply_mode(HVAC_TO_MODE[hvac_mode])

    async def _async_apply_mode(self, mode: str) -> None:
        """Start the unit in an operation mode.

        Args:
        ----
            mode: heat, cool, fan, dry or auto.

        """
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
        """Start the unit in its last operation mode; in auto mode it runs already."""
        if self._auto.enabled:
            return
        await self.coordinator.async_write(
            self._key,
            lambda name: self.coordinator.client.set_power(
                name, on=True, confirm=False
            ),
            running=True,
        )

    async def async_turn_off(self) -> None:
        """Stop the unit, and auto mode."""
        self._stop_auto()
        await self.coordinator.async_write(
            self._key,
            lambda name: self.coordinator.client.set_power(
                name, on=False, confirm=False
            ),
            running=False,
        )

    # -- auto mode ----------------------------------------------------------------

    @property
    def _outdoor_mean(self) -> float | None:
        outdoor = self.coordinator.outdoor
        return outdoor.mean_24h if outdoor else None

    async def async_added_to_hass(self) -> None:
        """Restore auto mode."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_extra_data()) is not None:
            self._auto.restore(data.as_dict())
            self._auto.notify()

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        """Return the state of auto mode, to restore after a restart."""
        return RestoredExtraData(self._auto.as_dict())

    @callback
    def _handle_coordinator_update(self) -> None:
        """Let auto mode act on the units as read."""
        self._run_auto()
        super()._handle_coordinator_update()

    def _run_auto(self) -> None:
        """Feed auto mode the room temperature, and make the change it decides."""
        auto, unit = self._auto, self.unit
        if not auto.enabled or self._auto_busy or unit is None:
            return
        if not self.coordinator.last_update_success:
            return  # the iTM was not read (e.g. busy web UI): nothing new to act on
        now = dt_util.utcnow()
        if unit.room_temperature is not None:
            auto.add_sample(now, unit.room_temperature)
        if self._auto_written is None or now - self._auto_written >= AUTO_WRITE_GRACE:
            if not unit.running or unit.mode != auto.mode:
                LOGGER.info(
                    "%s was changed outside auto mode (%s); auto mode stopped",
                    self.display_name,
                    unit.mode if unit.running else "off",
                )
                self._record(auto.stop(now, Reason.OVERRIDDEN, unit.room_temperature))
                return
            if auto.mode is not Mode.FAN and unit.setpoint is not None:
                if unit.setpoint == auto.target:
                    self._setpoint_synced = True
                elif self._setpoint_synced:
                    # Changed at the unit: someone wants another temperature.
                    auto.target = unit.setpoint
                    self._record(
                        auto.note(now, Reason.TARGET_CHANGED, unit.room_temperature)
                    )
        decision = auto.decide(now, self._outdoor_mean)
        sync = (
            not self._setpoint_synced
            and auto.mode is not Mode.FAN
            and not (auto.retry_at and now < auto.retry_at)
        )
        if decision is None and not sync:
            auto.notify()  # what it waits for may have changed
            return
        self._auto_busy = True
        self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_auto_change(decision),
            f"{DOMAIN} auto mode {self._key}",
        )

    async def _async_auto_change(self, decision: Decision | None) -> None:
        """Switch the unit to the mode auto mode chose, and set the target.

        Args:
        ----
            decision: The change to make, None to only set the target.

        """
        try:
            if decision is not None and decision.mode is not None:
                try:
                    await self._async_apply_mode(decision.mode)
                except HomeAssistantError as err:
                    LOGGER.warning(
                        "Auto mode could not switch %s to %s: %s",
                        self.display_name,
                        decision.mode,
                        err,
                    )
                    self._record(
                        self._auto.note(
                            dt_util.utcnow(), Reason.WRITE_FAILED, decision.temperature
                        )
                    )
                    return
                self._auto_written = dt_util.utcnow()
                self._auto.commit(decision)
                self._record(decision)
            await self._async_sync_setpoint()
        finally:
            self._auto_busy = False
            self.async_write_ha_state()

    async def _async_sync_setpoint(self, *, raise_errors: bool = False) -> None:
        """Make the target the unit's setpoint, while heating or cooling.

        Args:
        ----
            raise_errors: Raise a failed write instead of recording it.

        """
        unit, auto = self.unit, self._auto
        if unit is None or auto.mode in (None, Mode.FAN):
            return
        value = auto.target
        if unit.setpoint_range:
            value = min(max(value, unit.setpoint_range[0]), unit.setpoint_range[-1])
        if unit.setpoint != value:
            try:
                await self._async_set_setpoint(value)
            except HomeAssistantError as err:
                if raise_errors:
                    raise
                LOGGER.warning(
                    "Auto mode could not set %s to %s °C: %s",
                    self.display_name,
                    value,
                    err,
                )
                self._record(
                    auto.note(
                        dt_util.utcnow(), Reason.WRITE_FAILED, unit.room_temperature
                    )
                )
                return
            self._auto_written = dt_util.utcnow()
        self._setpoint_synced = True

    async def _async_start_auto(self, temperature: float | None) -> None:
        """Start auto mode.

        Args:
        ----
            temperature: The target; by default the unit's setpoint while it runs,
                else the last target.

        Raises:
        ------
            HomeAssistantError: The unit is unavailable.

        """
        unit = self.unit
        if unit is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unit_unavailable",
                translation_placeholders={"unit": self.display_name},
            )
        if temperature is not None:
            target = round(temperature)
        elif unit.running and unit.setpoint is not None:
            target = unit.setpoint
        else:
            target = self._auto.target
        target = round(min(max(target, self.min_temp), self.max_temp))
        decision = self._auto.start(
            dt_util.utcnow(),
            target,
            unit.room_temperature,
            self._outdoor_mean,
            unit.mode if unit.running else None,
        )
        self._auto_busy = True
        try:
            await self._async_apply_mode(decision.mode or decision.side)
            self._auto_written = dt_util.utcnow()
            self._auto.commit(decision)
            self._record(decision)
            self._setpoint_synced = False
            await self._async_sync_setpoint(raise_errors=True)
        finally:
            self._auto_busy = False
            self.async_write_ha_state()

    async def _async_set_auto_target(self, temperature: float) -> None:
        """Change the target of auto mode.

        Args:
        ----
            temperature: The new target.

        """
        unit = self.unit
        self._auto.target = round(temperature)
        self._record(
            self._auto.note(
                dt_util.utcnow(),
                Reason.TARGET_CHANGED,
                unit.room_temperature if unit else None,
            )
        )
        self._auto_busy = True
        try:
            await self._async_sync_setpoint(raise_errors=True)
        finally:
            self._auto_busy = False
            self.async_write_ha_state()

    def _stop_auto(self) -> None:
        """Stop auto mode, because another mode was chosen."""
        if self._auto.enabled:
            unit = self.unit
            self._record(
                self._auto.stop(
                    dt_util.utcnow(),
                    Reason.MANUAL,
                    unit.room_temperature if unit else None,
                )
            )

    def _record(self, decision: Decision) -> None:
        """Log a decision of auto mode, and fire it as an event for the logbook.

        Args:
        ----
            decision: The decision.

        """
        LOGGER.debug("Auto mode of %s: %s", self.display_name, decision)
        self.hass.bus.async_fire(
            EVENT_AUTO_MODE,
            {
                ATTR_ENTITY_ID: self.entity_id,
                "mode": decision.mode or "off",
                "reason": decision.reason,
                "target": decision.target,
                "temperature": _rounded(decision.temperature),
                "outdoor": _rounded(decision.outdoor),
            },
        )


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None
