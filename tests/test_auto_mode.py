"""Tests for the auto mode controller, including a simulated room."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.daikin_itm.auto_mode import (
    MAX_SWITCHES,
    AutoController,
    Decision,
    Mode,
    Reason,
    Waiting,
)

T0 = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def _running(mode: Mode, *, ventilate: bool = True, at: datetime = T0) -> AutoController:
    """Return a controller that has been in a mode for a long time."""
    auto = AutoController()
    auto.ventilate = ventilate
    long_ago = at - timedelta(hours=6)
    side = Mode.HEAT if mode is Mode.FAN else mode
    auto.commit(Decision(long_ago, mode, side, Reason.STARTED, 21, 21.0, None))
    auto.switches.clear()
    return auto


def _feed(auto: AutoController, temperature: float, minutes: int = 20) -> datetime:
    """Feed a steady temperature for a while, return the time of the last reading."""
    for i in range(minutes + 1):
        auto.add_sample(T0 + i * MINUTE, temperature)
    return T0 + minutes * MINUTE


@pytest.mark.parametrize(
    ("temperature", "outdoor", "current", "mode", "side"),
    [
        (19.0, 5.0, None, Mode.HEAT, Mode.HEAT),
        (21.2, 5.0, None, Mode.HEAT, Mode.HEAT),  # close to the target: let it heat
        (21.7, 5.0, None, Mode.FAN, Mode.HEAT),  # past the target: ventilate
        (24.0, 5.0, None, Mode.COOL, Mode.COOL),  # far too warm, even in winter
        (21.7, 25.0, None, Mode.COOL, Mode.COOL),
        (20.3, 25.0, None, Mode.FAN, Mode.COOL),
        (21.0, None, "cool", Mode.COOL, Mode.COOL),  # no weather: keep the unit's side
        (20.0, None, None, Mode.HEAT, Mode.HEAT),
        (None, 5.0, None, Mode.HEAT, Mode.HEAT),
    ],
)
def test_start(
    temperature: float | None,
    outdoor: float | None,
    current: str | None,
    mode: Mode,
    side: Mode,
) -> None:
    """Test the mode auto mode starts in."""
    decision = AutoController().start(T0, 21, temperature, outdoor, current)
    assert (decision.mode, decision.side, decision.reason) == (
        mode,
        side,
        Reason.STARTED,
    )


def test_start_without_ventilation() -> None:
    """Test auto mode keeps heating past the target without ventilation."""
    auto = AutoController()
    auto.ventilate = False
    assert auto.start(T0, 21, 21.7, 5.0, None).mode is Mode.HEAT


def test_waits_for_history() -> None:
    """Test no decision is taken on a few readings."""
    auto = _running(Mode.HEAT)
    now = _feed(auto, 25.0, minutes=10)
    assert auto.decide(now, None) is None
    assert auto.waiting is Waiting.HISTORY


@pytest.mark.parametrize(
    ("mode", "temperature", "outdoor", "expected", "reason"),
    [
        (Mode.HEAT, 21.0, None, None, None),
        (Mode.HEAT, 21.6, None, Mode.FAN, Reason.TARGET_REACHED),
        (Mode.HEAT, 23.0, None, Mode.COOL, Reason.TOO_WARM),
        (Mode.HEAT, 23.0, 5.0, Mode.FAN, Reason.TARGET_REACHED),  # cold outside
        (Mode.HEAT, 23.6, 5.0, Mode.COOL, Reason.TOO_WARM),
        (Mode.FAN, 21.0, None, None, None),
        (Mode.FAN, 20.4, None, Mode.HEAT, Reason.BELOW_TARGET),
        (Mode.FAN, 22.6, None, Mode.COOL, Reason.TOO_WARM),
        (Mode.COOL, 20.4, None, Mode.FAN, Reason.TARGET_REACHED),
        (Mode.COOL, 19.4, None, Mode.HEAT, Reason.TOO_COLD),
        (Mode.COOL, 19.4, 25.0, Mode.FAN, Reason.TARGET_REACHED),  # warm outside
    ],
)
def test_decide(
    mode: Mode,
    temperature: float,
    outdoor: float | None,
    expected: Mode | None,
    reason: Reason | None,
) -> None:
    """Test the changes auto mode makes for a steady room temperature."""
    auto = _running(mode)
    decision = auto.decide(_feed(auto, temperature), outdoor)
    if expected is None:
        assert decision is None
    else:
        assert decision is not None
        assert (decision.mode, decision.reason) == (expected, reason)


def test_fan_returns_to_cooling() -> None:
    """Test ventilating after cooling cools again when the room warms up."""
    auto = _running(Mode.COOL)
    auto.commit(auto._decision(T0 - timedelta(hours=1), Mode.FAN, Reason.TARGET_REACHED, 20.4, None))
    decision = auto.decide(_feed(auto, 21.6), None)
    assert decision is not None
    assert (decision.mode, decision.reason) == (Mode.COOL, Reason.ABOVE_TARGET)


def test_no_ventilation() -> None:
    """Test heating continues past the target without ventilation."""
    auto = _running(Mode.HEAT, ventilate=False)
    assert auto.decide(_feed(auto, 22.0), None) is None


def test_ventilation_turned_off_while_ventilating() -> None:
    """Test auto mode goes back to heating right away."""
    auto = _running(Mode.FAN)
    now = _feed(auto, 21.0, minutes=2)
    auto.switched_at = now  # just switched: no interval applies
    auto.ventilate = False
    decision = auto.decide(now, None)
    assert decision is not None
    assert (decision.mode, decision.reason) == (Mode.HEAT, Reason.VENTILATION_OFF)


def test_minimum_interval() -> None:
    """Test changes are at least MIN_SWITCH_INTERVAL apart."""
    auto = _running(Mode.FAN)
    now = _feed(auto, 20.0)
    auto.switched_at = now - timedelta(minutes=10)
    assert auto.decide(now, None) is None
    assert (auto.wanted, auto.waiting) == (Mode.HEAT, Waiting.INTERVAL)


def test_active_time_before_ventilating() -> None:
    """Test heating runs a while before it may switch to fan."""
    auto = _running(Mode.HEAT)
    now = _feed(auto, 21.8)
    auto.switched_at = now - timedelta(minutes=25)
    assert auto.decide(now, None) is None
    assert (auto.wanted, auto.waiting) == (Mode.FAN, Waiting.ACTIVE_TIME)


def test_changeover_dwell_falls_back_to_fan() -> None:
    """Test a changeover too soon after the last one ventilates instead."""
    auto = _running(Mode.HEAT)
    now = _feed(auto, 23.0)
    auto.side_since = now - timedelta(hours=1)
    decision = auto.decide(now, None)
    assert decision is not None
    assert decision.mode is Mode.FAN


def test_changeover_dwell_without_ventilation() -> None:
    """Test a changeover waits CHANGEOVER_DWELL."""
    auto = _running(Mode.HEAT, ventilate=False)
    now = _feed(auto, 23.0)
    auto.side_since = now - timedelta(hours=1)
    assert auto.decide(now, None) is None
    assert (auto.wanted, auto.waiting) == (Mode.COOL, Waiting.CHANGEOVER)


def test_no_changeover_while_heading_back() -> None:
    """Test no changeover to cooling while the room cools down by itself."""
    auto = _running(Mode.HEAT, ventilate=False)
    for i in range(21):
        auto.add_sample(T0 + i * MINUTE, 23.5 - i * 0.02)  # -1.2 °C per hour
    assert auto.decide(T0 + 20 * MINUTE, None) is None
    assert (auto.wanted, auto.waiting) == (Mode.COOL, Waiting.TREND)


def test_budget() -> None:
    """Test the daily number of changes, and the reserve ventilation leaves."""
    auto = _running(Mode.HEAT)
    now = _feed(auto, 21.8)
    auto.switches.extend([now - timedelta(hours=1)] * (MAX_SWITCHES - 4))
    assert auto.decide(now, None) is None
    assert (auto.wanted, auto.waiting) == (Mode.FAN, Waiting.BUDGET)

    auto = _running(Mode.FAN)
    now = _feed(auto, 20.0)
    auto.switches.extend([now - timedelta(hours=1)] * MAX_SWITCHES)
    assert auto.decide(now, None) is None
    assert auto.waiting is Waiting.BUDGET
    auto.switches.clear()
    auto.switches.extend([now - timedelta(hours=25)] * MAX_SWITCHES)  # expired
    assert auto.decide(now, None) is not None


def test_failed_write_waits() -> None:
    """Test a failed write is retried after MIN_SWITCH_INTERVAL."""
    auto = _running(Mode.FAN)
    now = _feed(auto, 20.0)
    auto.note(now, Reason.WRITE_FAILED, 20.0)
    assert auto.decide(now + MINUTE, None) is None
    assert auto.waiting is Waiting.INTERVAL
    for i in range(1, 22):
        auto.add_sample(now + i * MINUTE, 20.0)
    assert auto.decide(now + 21 * MINUTE, None) is not None


def test_stop_and_listeners() -> None:
    """Test stopping auto mode tells the listeners."""
    auto = _running(Mode.HEAT)
    calls: list[None] = []
    remove = auto.add_listener(lambda: calls.append(None))
    auto.stop(T0, Reason.MANUAL)
    assert not auto.enabled
    assert auto.mode is None
    assert auto.decide(T0, None) is None
    remove()
    auto.stop(T0, Reason.MANUAL)
    assert len(calls) == 1


def test_restore() -> None:
    """Test the state survives a restart, and unreadable state is ignored."""
    auto = _running(Mode.COOL)
    auto.switches.append(T0)
    auto.ventilate = False
    restored = AutoController()
    restored.restore(auto.as_dict())
    assert restored.as_dict() == auto.as_dict()
    assert restored.enabled

    broken = AutoController()
    broken.restore({"enabled": True, "mode": "warp"})
    assert not broken.enabled
    assert broken.mode is None


# -- simulation --------------------------------------------------------------------


@dataclass
class Room:
    """A room with one thermal mass, heated or cooled by a unit with a thermostat.

    The unit heats or cools at full power until the room is a little past its
    setpoint, then stops until it is a little before it (thermo-off), like a real
    indoor unit. Ventilating moves air only.
    """

    temperature: float
    setpoint: float = 21.0
    mode: Mode | None = None
    loss: float = 0.25  # 1/h towards the outdoor temperature
    power: float = 8.0  # °C/h at full power
    thermo_on: bool = True
    log: list[tuple[datetime, Mode]] = field(default_factory=list)

    def step(self, hours: float, outdoor: float, gains: float) -> None:
        """Advance the room by a time step."""
        delta = self.loss * (outdoor - self.temperature) + gains
        if self.mode is Mode.HEAT:
            self.thermo_on = self.temperature < self.setpoint + (
                0.5 if self.thermo_on else -0.5
            )
            delta += self.power if self.thermo_on else 0
        elif self.mode is Mode.COOL:
            self.thermo_on = self.temperature > self.setpoint - (
                0.5 if self.thermo_on else -0.5
            )
            delta -= self.power if self.thermo_on else 0
        self.temperature += delta * hours


def _simulate(
    days: int, outdoor: float, swing: float, gains: float, *, ventilate: bool = True
) -> tuple[Room, list[float]]:
    """Run auto mode against a room for a few days, one reading per minute."""
    room = Room(temperature=18.0)
    auto = AutoController()
    auto.ventilate = ventilate
    now = T0
    decision = auto.start(now, 21, room.temperature, outdoor, None)
    auto.commit(decision)
    room.mode = decision.mode
    room.log.append((now, room.mode))
    temperatures = []
    for _ in range(days * 24 * 60):
        hour = now.hour + now.minute / 60
        # Coldest at 05:00, warmest at 17:00; sun and people from 09:00 to 18:00.
        outside = outdoor + swing * math.sin((hour - 11) / 24 * 2 * math.pi)
        inside_gains = gains if 9 <= hour < 18 else 0.0
        room.step(1 / 60, outside, inside_gains)
        now += MINUTE
        auto.add_sample(now, round(room.temperature, 1))
        if (change := auto.decide(now, outdoor)) is not None:
            auto.commit(change)
            room.mode = change.mode
            room.log.append((now, room.mode))
        if now - T0 > timedelta(hours=3):
            temperatures.append(room.temperature)
    return room, temperatures


@pytest.mark.parametrize(
    ("outdoor", "swing", "gains"),
    [
        (2.0, 4.0, 1.0),  # winter
        (14.0, 6.0, 2.5),  # spring: cold nights, sunny days
        (26.0, 5.0, 2.0),  # summer
    ],
)
@pytest.mark.parametrize("ventilate", [True, False])
def test_simulated_days_stay_comfortable_without_bouncing(
    outdoor: float, swing: float, gains: float, *, ventilate: bool
) -> None:
    """Test a few simulated days: within comfort, few changes, no quick reversals."""
    room, temperatures = _simulate(4, outdoor, swing, gains, ventilate=ventilate)
    assert min(temperatures) > 18.9
    assert max(temperatures) < 23.6
    switches = len(room.log) - 1
    assert switches / 4 <= MAX_SWITCHES
    # Never from heating to cooling (or back) within two hours.
    sides = [(t, Mode.HEAT if m is Mode.HEAT else Mode.COOL) for t, m in room.log if m is not Mode.FAN]
    for (t1, s1), (t2, s2) in zip(sides, sides[1:], strict=False):
        if s1 is not s2:
            assert t2 - t1 >= timedelta(hours=2)
