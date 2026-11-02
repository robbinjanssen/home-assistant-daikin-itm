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
from .const import DOMAIN, EVENT_AUTO_MODE, LOGGER
from .entity import ItmEntity, add_entities_for_new_units, rounded, supports_auto

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
    entity's AutoController chooses heat, cool or fan on every new read of the iTM,
    and the target temperature is the controller's. Auto mode stops when another
    mode is chosen, or when the unit is changed elsewhere (remote controller, iTM).
    A setpoint changed elsewhere becomes the new target.

    Auto mode judges the unit only once no change Home Assistant made is pending in
    the coordinator, so the unit as read shows its own writes. After a mode change
    it sets the setpoint on the next read, when the unit shows the new mode and the
    setpoints it allows in that mode.
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
        self._auto_writing = False  # a background write for auto mode is running
        self._auto_service = False  # a service call is changing auto mode
        self._seen_read: datetime | None = None  # the read auto mode last acted on
        # The setpoint auto mode expects the unit to show in its current mode: the
        # last one it wrote or found. None until known, after a mode change and after
        # a failed write. Another value at the unit was set elsewhere.
        self._expected_setpoint: int | None = None

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
        if hvac_mode is not None:
            # Home Assistant doesn't check the mode of set_temperature.
            self._check_mode(hvac_mode)
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

        """
        self._check_mode(hvac_mode)
        if hvac_mode == HVACMode.AUTO:
            if not self._auto.enabled:
                await self._async_start_auto(None)
            return
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        self._stop_auto()
        await self._async_apply_mode(HVAC_TO_MODE[hvac_mode])

    def _check_mode(self, hvac_mode: HVACMode) -> None:
        """Refuse a mode the unit does not offer.

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

    @property
    def _fan_offered(self) -> bool:
        return self.caps is not None and "fan" in self.caps.modes

    def _setpoint_for(self, target: int) -> int:
        """Return the setpoint for a target: the nearest the unit allows now."""
        return round(min(max(target, self.min_temp), self.max_temp))

    async def async_added_to_hass(self) -> None:
        """Restore auto mode, and act on its settings when they change."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_extra_data()) is not None:
            self._auto.restore(data.as_dict())
            self._auto.notify()
        self.async_on_remove(
            self._auto.on_request(lambda: self._run_auto(sample=False))
        )

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        """Return the state of auto mode, to restore after a restart."""
        return RestoredExtraData(self._auto.as_dict())

    @callback
    def _handle_coordinator_update(self) -> None:
        """Let auto mode act on each new read of the iTM.

        Not on the updates that show a change Home Assistant made, nor on the old
        data the coordinator keeps showing while logins are refused.
        """
        read_at = self.coordinator.read_at
        if read_at is not None and read_at != self._seen_read:
            self._seen_read = read_at
            self._run_auto(sample=True)
        super()._handle_coordinator_update()

    def _run_auto(self, *, sample: bool) -> None:
        """Make the change auto mode decides.

        Args:
        ----
            sample: Feed auto mode the room temperature: the unit was just read.

        """
        auto, unit, now = self._auto, self.unit, dt_util.utcnow()
        if not auto.enabled or unit is None:
            return
        if sample and unit.room_temperature is not None:
            auto.add_sample(now, unit.room_temperature)
        if (
            self._auto_writing
            or self._auto_service
            or self.coordinator.has_pending(self._key)
        ):
            return  # judge the unit once it shows the change on its way
        if not unit.running or unit.mode != auto.mode:
            LOGGER.info(
                "%s was changed outside auto mode (%s); auto mode stopped",
                self.display_name,
                unit.mode if unit.running else "off",
            )
            self._record(auto.stop(now, Reason.OVERRIDDEN, unit.room_temperature))
            self.async_write_ha_state()
            return
        self._check_setpoint(now)
        auto.fan_offered = self._fan_offered
        decision = auto.decide(now, self._outdoor_mean)
        if decision is None and not self._setpoint_to_write(now):
            return
        self._auto_writing = True
        self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_auto_change(decision, auto.run),
            f"{DOMAIN} auto mode {self._key}",
        )

    def _check_setpoint(self, now: datetime) -> None:
        """Take a setpoint changed at the unit as the new target."""
        auto, unit = self._auto, self.unit
        if unit is None or unit.setpoint is None:
            return
        expected = self._expected_setpoint
        if expected is None:
            # The unit keeps its setpoint while ventilating; it should show the
            # target (or the nearest it allows) while heating or cooling.
            if auto.mode is Mode.FAN or unit.setpoint == self._setpoint_for(
                auto.target
            ):
                self._expected_setpoint = unit.setpoint
            return
        if unit.setpoint != expected:
            LOGGER.info(
                "The setpoint of %s was changed outside auto mode to %s °C",
                self.display_name,
                unit.setpoint,
            )
            self._expected_setpoint = auto.target = unit.setpoint
            self._record(auto.note(now, Reason.TARGET_CHANGED, unit.room_temperature))

    def _setpoint_to_write(self, now: datetime) -> bool:
        """Return if the unit should get the target as its setpoint now."""
        auto, unit = self._auto, self.unit
        return (
            unit is not None
            and auto.mode in (Mode.HEAT, Mode.COOL)
            and unit.setpoint != self._setpoint_for(auto.target)
            and auto.can_write(now)
        )

    def _still_running(self, run: int) -> bool:
        """Return if auto mode still runs as it did when a write started."""
        return self._auto.enabled and self._auto.run == run

    async def _async_auto_change(self, decision: Decision | None, run: int) -> None:
        """Switch the unit to the mode auto mode chose, or set the target.

        Args:
        ----
            decision: The change to make, None to set the target as setpoint.
            run: The run of auto mode that decided.

        """
        try:
            if decision is None:
                await self._async_write_target(run)
                return
            mode = decision.mode or decision.side
            try:
                await self._async_apply_mode(mode)
            except HomeAssistantError as err:
                if self._still_running(run):
                    LOGGER.warning(
                        "Auto mode could not switch %s to %s: %s",
                        self.display_name,
                        mode,
                        err,
                    )
                    self._record(
                        self._auto.note(
                            dt_util.utcnow(), Reason.WRITE_FAILED, decision.temperature
                        )
                    )
                return
            if not self._still_running(run):
                return  # stopped while switching: the user's choice stands
            self._expected_setpoint = None  # the setpoint follows on the next read
            self._auto.commit(decision)
            self._record(decision)
        finally:
            self._auto_writing = False
            self.async_write_ha_state()

    async def _async_write_target(self, run: int) -> None:
        """Write the target as the unit's setpoint, recording a failure.

        Args:
        ----
            run: The run of auto mode that decided.

        """
        value = self._setpoint_for(self._auto.target)
        try:
            await self._async_set_setpoint(value)
        except HomeAssistantError as err:
            self._expected_setpoint = None
            if self._still_running(run):
                LOGGER.warning(
                    "Auto mode could not set %s to %s °C: %s",
                    self.display_name,
                    value,
                    err,
                )
                self._record(
                    self._auto.note(
                        dt_util.utcnow(), Reason.WRITE_FAILED, self.current_temperature
                    )
                )
            return
        self._expected_setpoint = value
        self._auto.count_write(dt_util.utcnow())

    async def _async_start_auto(self, temperature: float | None) -> None:
        """Start auto mode: switch the unit to the mode it chooses.

        The setpoint follows on the next read, when the unit shows the mode and the
        setpoints it allows in it.

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
        self._auto.fan_offered = self._fan_offered
        decision = self._auto.start(
            dt_util.utcnow(),
            self._setpoint_for(target),
            unit.room_temperature,
            self._outdoor_mean,
            unit.mode if unit.running else None,
        )
        self._auto_service = True
        try:
            await self._async_apply_mode(decision.mode or decision.side)
        finally:
            self._auto_service = False
        self._expected_setpoint = None
        self._auto.commit(decision)
        self._record(decision)
        self.async_write_ha_state()

    async def _async_set_auto_target(self, temperature: float) -> None:
        """Change the target of auto mode, and the setpoint while heating or cooling.

        Args:
        ----
            temperature: The new target.

        """
        auto = self._auto
        auto.target = round(temperature)
        self._record(
            auto.note(dt_util.utcnow(), Reason.TARGET_CHANGED, self.current_temperature)
        )
        unit = self.unit
        value = self._setpoint_for(auto.target)
        try:
            if (
                unit is not None
                and auto.mode in (Mode.HEAT, Mode.COOL)
                and unit.setpoint != value
            ):
                self._auto_service = True
                try:
                    await self._async_set_setpoint(value)
                except HomeAssistantError:
                    self._expected_setpoint = None  # auto mode tries again later
                    raise
                finally:
                    self._auto_service = False
                self._expected_setpoint = value
        finally:
            self.async_write_ha_state()

    def _stop_auto(self) -> None:
        """Stop auto mode, because another mode was chosen."""
        if self._auto.enabled:
            self._record(
                self._auto.stop(
                    dt_util.utcnow(), Reason.MANUAL, self.current_temperature
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
                "temperature": rounded(decision.temperature),
                "outdoor": rounded(decision.outdoor),
            },
        )
