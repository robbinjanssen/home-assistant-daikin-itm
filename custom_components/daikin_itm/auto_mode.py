"""Auto mode: keep a room at a target temperature by choosing heat, cool or fan.

An indoor unit keeps the room at its setpoint itself: it stops heating or cooling
once it gets there (thermo-off). Auto mode only chooses the operation mode, and
keeps the target as the setpoint while heating or cooling, so a change of mode is
a single write.

The controller has no Home Assistant or iTM dependencies: the climate entity feeds
it room temperatures, asks it what to do and writes the result. It changes mode
rarely, because indoor units allow a limited number of setting changes (Daikin
mentions about 7000 a year):

- it compares the 20-minute average room temperature with the target, not single
  readings;
- it changes over between heating and cooling only far from the target
  (CHANGEOVER_MARGIN), not while the room is already heading back, and not within
  CHANGEOVER_DWELL of the previous changeover. A cold (warm) 24-hour average outdoor
  temperature makes cooling (heating) harder to reach;
- with ventilation enabled it switches to fan once the room is past the target
  (FAN_MARGIN), and back when it drifts the other way. Ventilating stops when only
  FAN_RESERVE changes are left for the day;
- it makes at most MAX_SWITCHES changes in 24 hours, at least MIN_SWITCH_INTERVAL
  apart.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_TARGET = 21

# Degrees from the target, compared with the average room temperature.
FAN_MARGIN = 0.5
CHANGEOVER_MARGIN = 1.5
# Below COLD_OUTSIDE (24-hour average) cooling needs WEATHER_MARGIN more degrees,
# above WARM_OUTSIDE heating does.
COLD_OUTSIDE = 12.0
WARM_OUTSIDE = 20.0
WEATHER_MARGIN = 1.0
# Starting auto mode: the season by the 24-hour average outdoor temperature.
SEASON_SPLIT = 16.0

AVERAGE_WINDOW = timedelta(minutes=20)
# Decisions wait until the readings cover this much of the window, e.g. after a restart.
MIN_HISTORY = timedelta(minutes=15)
# Readings closer together are skipped (the entity also updates on its own writes).
SAMPLE_INTERVAL = timedelta(seconds=30)
# °C per hour: no changeover to cooling while the room cools faster, and the reverse.
TREND_LIMIT = 0.3

MIN_SWITCH_INTERVAL = timedelta(minutes=20)
MIN_ACTIVE_TIME = timedelta(minutes=30)  # heating or cooling before ventilating
CHANGEOVER_DWELL = timedelta(hours=2)
MAX_SWITCHES = 12  # per 24 hours
FAN_RESERVE = 4
BUDGET_PERIOD = timedelta(hours=24)


class Mode(StrEnum):
    """An operation mode auto mode chooses."""

    HEAT = "heat"
    COOL = "cool"
    FAN = "fan"


class Reason(StrEnum):
    """Why auto mode made a decision."""

    STARTED = "started"
    TARGET_REACHED = "target_reached"  # heating or cooling -> fan
    BELOW_TARGET = "below_target"  # fan -> heating again
    ABOVE_TARGET = "above_target"  # fan -> cooling again
    TOO_COLD = "too_cold"  # changeover to heating
    TOO_WARM = "too_warm"  # changeover to cooling
    VENTILATION_OFF = "ventilation_off"  # fan -> heating or cooling
    TARGET_CHANGED = "target_changed"  # setpoint changed at the unit
    WRITE_FAILED = "write_failed"
    MANUAL = "manual"  # another mode chosen in Home Assistant
    OVERRIDDEN = "overridden"  # changed at the unit or in the iTM


class Waiting(StrEnum):
    """Why auto mode holds back a change it wants."""

    HISTORY = "history"
    INTERVAL = "interval"
    ACTIVE_TIME = "active_time"
    CHANGEOVER = "changeover"
    TREND = "trend"
    BUDGET = "budget"


@dataclass(frozen=True)
class Decision:
    """A choice of auto mode, or that it stopped (mode None)."""

    at: datetime
    mode: Mode | None
    side: Mode  # heating or cooling, also while ventilating
    reason: Reason
    target: int
    temperature: float | None  # the (average) room temperature it was based on
    outdoor: float | None  # the 24-hour average outdoor temperature

    def as_dict(self) -> dict[str, Any]:
        """Return the decision for storage."""
        return {
            "at": self.at.isoformat(),
            "mode": self.mode,
            "side": self.side,
            "reason": self.reason,
            "target": self.target,
            "temperature": self.temperature,
            "outdoor": self.outdoor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Decision:
        """Return a stored decision.

        Args:
        ----
            data: What as_dict returned.

        Returns:
        -------
            The decision.

        """
        return cls(
            at=datetime.fromisoformat(data["at"]),
            mode=Mode(data["mode"]) if data["mode"] else None,
            side=Mode(data["side"]),
            reason=Reason(data["reason"]),
            target=int(data["target"]),
            temperature=data["temperature"],
            outdoor=data["outdoor"],
        )


def _thresholds(outdoor: float | None) -> tuple[float, float]:
    """Return the deviations from the target to change over to heating and cooling."""
    heat_at, cool_at = -CHANGEOVER_MARGIN, CHANGEOVER_MARGIN
    if outdoor is not None and outdoor >= WARM_OUTSIDE:
        heat_at -= WEATHER_MARGIN
    if outdoor is not None and outdoor <= COLD_OUTSIDE:
        cool_at += WEATHER_MARGIN
    return heat_at, cool_at


class AutoController:  # pylint: disable=too-many-instance-attributes
    """Auto mode of one indoor unit."""

    def __init__(self) -> None:
        """Initialize auto mode, switched off."""
        self.enabled = False
        self.target = DEFAULT_TARGET
        self.ventilate = True
        self.mode: Mode | None = None
        self.side = Mode.HEAT
        self.switched_at: datetime | None = None  # when the current mode was chosen
        self.side_since: datetime | None = None  # when the current side was chosen
        self.switches: deque[datetime] = deque()
        self.last: Decision | None = None
        self.waiting: Waiting | None = None
        self.wanted: Mode | None = None
        self.retry_at: datetime | None = None
        self._samples: deque[tuple[datetime, float]] = deque()
        self._listeners: list[Callable[[], None]] = []

    # -- listeners ------------------------------------------------------------------
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call listener whenever auto mode changes.

        Args:
        ----
            listener: Called without arguments.

        Returns:
        -------
            Removes the listener.

        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def notify(self) -> None:
        """Tell the listeners auto mode changed."""
        for listener in list(self._listeners):
            listener()

    # -- room temperature -----------------------------------------------------------
    def add_sample(self, now: datetime, temperature: float) -> None:
        """Remember a room temperature reading.

        Args:
        ----
            now: When it was read.
            temperature: The room temperature.

        """
        if self._samples and now - self._samples[-1][0] < SAMPLE_INTERVAL:
            return
        self._samples.append((now, temperature))
        while self._samples and now - self._samples[0][0] > AVERAGE_WINDOW:
            self._samples.popleft()

    def average(self, now: datetime) -> float | None:
        """Return the average room temperature, None until there are enough readings.

        Args:
        ----
            now: The current time.

        Returns:
        -------
            The average over AVERAGE_WINDOW.

        """
        samples = [(t, v) for t, v in self._samples if now - t <= AVERAGE_WINDOW]
        if not samples or now - samples[0][0] < MIN_HISTORY:
            return None
        return sum(v for _, v in samples) / len(samples)

    def trend(self, now: datetime) -> float:
        """Return how fast the room temperature changes, in °C per hour.

        Args:
        ----
            now: The current time.

        Returns:
        -------
            The least-squares slope over AVERAGE_WINDOW, 0 with too few readings.

        """
        samples = [
            ((t - now).total_seconds() / 3600, v)
            for t, v in self._samples
            if now - t <= AVERAGE_WINDOW
        ]
        if len(samples) < 3:
            return 0.0
        mean_x = sum(x for x, _ in samples) / len(samples)
        mean_y = sum(y for _, y in samples) / len(samples)
        var = sum((x - mean_x) ** 2 for x, _ in samples)
        if not var:
            return 0.0
        return sum((x - mean_x) * (y - mean_y) for x, y in samples) / var

    def switches_today(self, now: datetime) -> int:
        """Return the number of mode changes in the last 24 hours.

        Args:
        ----
            now: The current time.

        Returns:
        -------
            The number of changes.

        """
        while self.switches and now - self.switches[0] >= BUDGET_PERIOD:
            self.switches.popleft()
        return len(self.switches)

    # -- decisions ------------------------------------------------------------------
    def start(
        self,
        now: datetime,
        target: int,
        temperature: float | None,
        outdoor: float | None,
        current: str | None,
    ) -> Decision:
        """Choose the mode to start auto mode in.

        Args:
        ----
            now: The current time.
            target: The temperature to keep.
            temperature: The room temperature now.
            outdoor: The 24-hour average outdoor temperature, None when unknown.
            current: The unit's current mode, None when it is stopped.

        Returns:
        -------
            The decision; commit it once the unit is in its mode.

        """
        if outdoor is not None:
            side = Mode.HEAT if outdoor < SEASON_SPLIT else Mode.COOL
        elif current in (Mode.HEAT, Mode.COOL):
            side = Mode(current)
        else:
            side = (
                Mode.HEAT if temperature is None or temperature < target else Mode.COOL
            )
        mode = side
        if temperature is not None:
            deviation = temperature - target
            heat_at, cool_at = _thresholds(outdoor)
            if side is Mode.HEAT and deviation >= cool_at:
                side = mode = Mode.COOL
            elif side is Mode.COOL and deviation <= heat_at:
                side = mode = Mode.HEAT
            elif self.ventilate and (
                deviation >= FAN_MARGIN
                if side is Mode.HEAT
                else deviation <= -FAN_MARGIN
            ):
                mode = Mode.FAN
        return Decision(now, mode, side, Reason.STARTED, target, temperature, outdoor)

    def decide(self, now: datetime, outdoor: float | None) -> Decision | None:
        """Return the change to make now, if any.

        Sets waiting and wanted when a change is held back.

        Args:
        ----
            now: The current time.
            outdoor: The 24-hour average outdoor temperature, None when unknown.

        Returns:
        -------
            The decision; commit it once the unit is in its mode.

        """
        self.waiting = self.wanted = None
        if not self.enabled or self.mode is None:
            return None
        average = self.average(now)
        if self.mode is Mode.FAN and not self.ventilate:
            # The user turned ventilation off: no reason to wait.
            return self._decision(
                now, self.side, Reason.VENTILATION_OFF, average, outdoor
            )
        if average is None:
            self.waiting = Waiting.HISTORY
            return None
        held: tuple[Mode, Waiting] | None = None
        for mode, reason, changeover in self._candidates(
            average - self.target, outdoor
        ):
            blocked = self._blocked(now, mode, changeover=changeover)
            if blocked is None:
                return self._decision(now, mode, reason, average, outdoor)
            held = held or (mode, blocked)
        if held:
            self.wanted, self.waiting = held
        return None

    def _candidates(
        self, deviation: float, outdoor: float | None
    ) -> list[tuple[Mode, Reason, bool]]:
        """Return the changes the room asks for, most wanted first.

        Each is (mode, reason, whether it is a changeover between heating and
        cooling).
        """
        heat_at, cool_at = _thresholds(outdoor)
        if self.side is Mode.HEAT:
            other, reason, past = Mode.COOL, Reason.TOO_WARM, deviation >= cool_at
            ahead = deviation >= FAN_MARGIN  # past the target: ventilate
            behind = deviation <= -FAN_MARGIN  # back below it: heat again
            back = Reason.BELOW_TARGET
        else:
            other, reason, past = Mode.HEAT, Reason.TOO_COLD, deviation <= heat_at
            ahead = deviation <= -FAN_MARGIN
            behind = deviation >= FAN_MARGIN
            back = Reason.ABOVE_TARGET
        candidates = []
        if past:
            candidates.append((other, reason, True))
        if self.mode is not Mode.FAN and self.ventilate and ahead:
            candidates.append((Mode.FAN, Reason.TARGET_REACHED, False))
        if self.mode is Mode.FAN and behind:
            candidates.append((self.side, back, False))
        return candidates

    def _blocked(
        self, now: datetime, mode: Mode, *, changeover: bool
    ) -> Waiting | None:
        """Return why a change to mode must wait, None when it can be made now."""
        if (self.retry_at and now < self.retry_at) or (
            self.switched_at and now - self.switched_at < MIN_SWITCH_INTERVAL
        ):
            return Waiting.INTERVAL
        if changeover and (waiting := self._changeover_blocked(now, mode)):
            return waiting
        if (
            mode is Mode.FAN
            and self.switched_at
            and (now - self.switched_at < MIN_ACTIVE_TIME)
        ):
            return Waiting.ACTIVE_TIME
        left = MAX_SWITCHES - self.switches_today(now)
        if left <= 0 or (mode is Mode.FAN and left <= FAN_RESERVE):
            return Waiting.BUDGET
        return None

    def _changeover_blocked(self, now: datetime, mode: Mode) -> Waiting | None:
        """Return why a changeover to mode must wait, None when it can be made now."""
        if self.side_since and now - self.side_since < CHANGEOVER_DWELL:
            return Waiting.CHANGEOVER
        rising = self.trend(now)
        if (mode is Mode.COOL and rising < -TREND_LIMIT) or (
            mode is Mode.HEAT and rising > TREND_LIMIT
        ):
            return Waiting.TREND
        return None

    def _decision(
        self,
        now: datetime,
        mode: Mode,
        reason: Reason,
        temperature: float | None,
        outdoor: float | None,
    ) -> Decision:
        side = self.side if mode is Mode.FAN else mode
        return Decision(now, mode, side, reason, self.target, temperature, outdoor)

    # -- state changes --------------------------------------------------------------
    def commit(self, decision: Decision) -> None:
        """Take a decision once the unit is in its mode.

        Args:
        ----
            decision: What start or decide returned.

        """
        if decision.mode is None:
            self.enabled = False
        else:
            self.enabled = True
            self.target = decision.target
            started = decision.reason is Reason.STARTED
            if started or decision.mode is not self.mode:
                self.switched_at = decision.at
                self.switches.append(decision.at)
            if started or decision.side is not self.side:
                self.side_since = decision.at
            self.side = decision.side
        self.mode = decision.mode
        self.last = decision
        self.waiting = self.wanted = self.retry_at = None
        self.notify()

    def stop(
        self, now: datetime, reason: Reason, temperature: float | None = None
    ) -> Decision:
        """Switch auto mode off.

        Args:
        ----
            now: The current time.
            reason: Why.
            temperature: The room temperature.

        Returns:
        -------
            The decision, already taken.

        """
        decision = Decision(
            now, None, self.side, reason, self.target, temperature, None
        )
        self.commit(decision)
        return decision

    def note(
        self, now: datetime, reason: Reason, temperature: float | None
    ) -> Decision:
        """Record an event that does not change the mode, e.g. a failed write.

        Args:
        ----
            now: The current time.
            reason: What happened.
            temperature: The room temperature.

        Returns:
        -------
            The recorded decision.

        """
        decision = Decision(
            now, self.mode, self.side, reason, self.target, temperature, None
        )
        self.last = decision
        if reason is Reason.WRITE_FAILED:
            self.retry_at = now + MIN_SWITCH_INTERVAL
        self.notify()
        return decision

    # -- storage --------------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        """Return the state to restore after a restart."""
        return {
            "enabled": self.enabled,
            "target": self.target,
            "ventilate": self.ventilate,
            "mode": self.mode,
            "side": self.side,
            "switched_at": self.switched_at.isoformat() if self.switched_at else None,
            "side_since": self.side_since.isoformat() if self.side_since else None,
            "switches": [t.isoformat() for t in self.switches],
            "last": self.last.as_dict() if self.last else None,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Restore the state as_dict returned; ignores what it can't read.

        Args:
        ----
            data: The stored state.

        """
        try:
            mode = Mode(data["mode"]) if data["mode"] else None
            state = {
                "enabled": bool(data["enabled"]) and mode is not None,
                "target": int(data["target"]),
                "ventilate": bool(data["ventilate"]),
                "mode": mode,
                "side": Mode(data["side"]),
                "switched_at": _time(data["switched_at"]),
                "side_since": _time(data["side_since"]),
                "switches": deque(datetime.fromisoformat(t) for t in data["switches"]),
                "last": Decision.from_dict(data["last"]) if data["last"] else None,
            }
        except KeyError, TypeError, ValueError:
            return
        for name, value in state.items():
            setattr(self, name, value)


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
