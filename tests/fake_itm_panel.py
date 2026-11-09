"""The simulated iTM's icon and list views, operation panel and settings dialog."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, cast

from .fake_itm_base import (
    CAPACITY_OPTIONS,
    EXTRA_COMBOS,
    EXTRA_CONTROLS,
    EXTRA_NUMBERS,
    EXTRA_OWNER,
    EXTRA_RADIOS,
    LIST_HEADERS,
    LOCK_LABELS,
    LOW_NOISE_OPTIONS,
    MULTISTATE_OPTIONS,
    POWER_OPTIONS,
    SETTINGS_TABS,
    FakeAnalog,
    FakeITMBase,
    FakeUnit,
    FakeVentilation,
    FakeZone,
    Update,
    _enabled,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class PanelMixin(FakeITMBase):
    """The icon and list views, the operation panel and the settings dialog."""

    def _select_icon(self, ev: dict[str, Any]) -> list[Update]:
        self.selected = ev["number"]
        return [("UPDATE", 1, [], []), ("UPDATE", 4, [], self._panel_delta())]

    def _zone_down(self, _ev: dict[str, Any]) -> list[Update]:
        self.path.append(cast("FakeZone", self.path[-1].children[self.selected]))
        self.selected = 0
        return [
            ("UPDATE", 1, [], [(6, "ICONPANEL", self._icons())]),
            ("UPDATE", 4, [], self._panel_delta()),
        ]

    def _choose(self, ev: dict[str, Any]) -> list[Update]:
        """Pick on/off (part 9) or a setpoint (part 11) on the operation panel."""
        self._check_overlap()
        item = self._item()
        if ev["part"] == 11:
            self.pending_setpoint = item.setpoint_range[ev["number"]]
        elif ev["number"] in {1, 2} and isinstance(item, FakeUnit):
            # Setback asks first, like the real iTM; only Yes makes it pending.
            self.confirming_setback = ev["number"]
            text = "Setback low?" if ev["number"] == 1 else "Setback high?"
            confirm = [24, 2, text, "Confirm", "Yes", "No"]
            return [("DLG_OPEN", 4, confirm, [])]
        else:
            self.pending_power = ev["number"]
        # The client already shows its own pick.
        self.shown[ev["part"], "SINGLE_SELIDX"] = ev["number"]
        return [("UPDATE", 4, [], [])]

    def _confirm_setback(self, ev: dict[str, Any]) -> list[Update]:
        """Answer the setback confirm: Yes (CLICK) or No (CLICK2)."""
        number, self.confirming_setback = self.confirming_setback, None
        if ev["event"] == 1:
            self.pending_power = number
            self.shown[9, "SINGLE_SELIDX"] = number
        return [("UPDATE", 4, [], [])]

    def _list_view(self, _ev: dict[str, Any]) -> list[Update]:
        """Show the list view (screen 2) with every unit of the current zone."""
        self.list_shown = True
        rows = [self._list_row(c) for c in self.path[-1].children]
        panel = {
            "cols": [[name, 100] for name in LIST_HEADERS],
            "sort": [0, 0],
            "rows": rows,
            "selected": self.selected,
        }
        return [("SHOW", 2, [], [(8, "PNTLIST", panel)]), ("HIDE", 1, [], [])]

    def _icon_view(self, _ev: dict[str, Any]) -> list[Update]:
        self.list_shown = False
        return [("SHOW", 1, [], []), ("HIDE", 2, [], [])]

    @staticmethod
    def _list_row(c: Any) -> list[list[Any]]:
        """One list view row: [text, icon, state] per column."""
        if isinstance(c, FakeZone):
            return [[c.name, -1, -1], ["", 1000, 0], *([["", -1, -1]] * 23)]
        if not isinstance(c, FakeUnit):
            icon = 2201 if isinstance(c, FakeVentilation) else 2601
            return [[c.name, -1, -1], ["", icon, 0], *([["", -1, -1]] * 23)]
        cells: list[list[Any]] = [["", -1, -1] for _ in LIST_HEADERS]
        cells[0] = [c.name, -1, -1]
        cells[1] = ["", 2114, 0 if c.running else 2]
        cells[2] = ["", 4000, -1]
        cells[3] = [c.mode, -1, -1]
        cells[4] = [f"{c.temp:.1f}°C", -1, -1]
        cells[5] = ["---", -1, -1]
        cells[6] = [f"{c.setpoint:.1f}°C" if c.running else "---", -1, -1]
        for col, (enabled, low, high) in ((7, c.cool_limit), (8, c.heat_limit)):
            cells[col] = [f"{low:.1f}°C-{high:.1f}°C" if enabled else "", -1, -1]
        cells[11] = ["", (c.fan_icon or 3011) - 1, -1]
        cells[9] = ["", 4013, -1] if c.filter_sign else ["", -1, -1]
        cells[10] = ["", 4015, -1] if c.dust_alarm else ["", -1, -1]
        for col in range(13, 19):
            cells[col] = ["---", -1, -1]
        cells[12] = ["", c.airflow - 1, -1] if c.airflow else ["---", -1, -1]
        cells[19] = [LOCK_LABELS[c.lock_start_stop], -1, -1]
        cells[20] = [LOCK_LABELS[c.lock_mode], -1, -1]
        cells[21] = [LOCK_LABELS[c.lock_setpoint], -1, -1]
        cells[23] = [c.error_code, -1, -1]
        cells[24] = ["---", -1, -1]
        return cells

    def _details(self, _ev: dict[str, Any]) -> list[Update]:
        item = self.path[-1].children[self.selected]
        if isinstance(item, FakeAnalog):
            chars = (
                "Area:\nFloor 1&gt;Building&gt;Analog\n\n"
                f"Target Point:{item.source_point}\nTarget analog value:Suction Temp.\n"
                f"Upper Limit:{item.upper_limit:.1f}\nUnit Label:°C\n"
            )
            parts = [
                (2, "TEXT", item.name),
                (4, "TEXT", item.point_id),
                (6, "TEXT", "Internal Ai"),
                (7, "VISIBLED", 0),
                (8, "VISIBLED", 0),
                (9, "VISIBLED", 0),
                (10, "VISIBLED", 0),
                (12, "TEXT", ""),
                (14, "TEXT", chars),
            ]
            return [("OPEN", 9, [-1], parts)]
        if isinstance(item, FakeVentilation):
            parts = [
                (2, "TEXT", item.name),
                (4, "TEXT", item.point_id),
                (6, "TEXT", "Ventilator"),
                (8, "TEXT", "1"),
                (10, "TEXT", item.address),
                (12, "TEXT", item.description),
                (14, "TEXT", "Area:\nFloor 1&gt;Building&gt;Ventilation\n\n"),
            ]
            return [("OPEN", 9, [-1], parts)]
        u = self._item()
        chars = (
            "Area:\nFloor 1&gt;Building&gt;Indoor units\n\n"
            f"Thermostat Status [{'ON' if u.thermo_on else 'OFF'}]\n"
            "R32 Alarm [Disable]\n"
        )
        parts = [
            (2, "TEXT", u.name),
            (4, "TEXT", u.point_id),
            (6, "TEXT", "Indoor unit"),
            (8, "TEXT", "1"),
            (10, "TEXT", u.address),
            (12, "TEXT", u.description),
            (14, "TEXT", chars),
        ]
        return [("OPEN", 9, [-1], parts)]

    def _commit(self, _ev: dict[str, Any]) -> list[Update]:
        targets = self._operated_units()
        power, setpoint = self.pending_power, self.pending_setpoint

        def change(targets: list[FakeUnit] = targets) -> None:
            for item in targets:
                if power is not None:
                    item.running = power == 0
                    item.setback = power if power in {1, 2} else 0
                if setpoint is not None and item.running:
                    item.setpoint = setpoint

        self._later(change)
        self.pending_setpoint = self.pending_power = None
        return []

    # -- settings dialog (screen 7) -------------------------------------------------
    def _open_dialog(self, _ev: dict[str, Any]) -> list[Update]:
        self._check_overlap()
        item = self.path[-1].children[self.selected]
        if not isinstance(item, (FakeUnit, FakeZone)):
            # A ventilation unit or analog point: on/off at most, no mode nor fan.
            self.dialog = {"ticks": set(), "power": None, "mode": None, "fan": None}
            running = getattr(item, "running", False)
            other: list[tuple[int, str, Any]] = [
                (0, "LABEL", "Detailed Setup : " + item.name),
                (2, "LABEL", "On/Off"),
                (3, "CHECK_STATE", 1 if running else 0),
                (4, "CHECK_STATE", 0 if running else 1),
                (18, "COMBO_STRS", []),
                (18, "VISIBLED", 0),
                (28, "VISIBLED", 0),
            ]
            for check, combo, options, chosen in (
                (33, 34, "vent_modes", "vent_mode"),
                (35, 36, "vent_amounts", "vent_amount"),
            ):
                offered = getattr(item, options, ())
                other.append((check, "VISIBLED", 1 if offered else 0))
                if offered:
                    other.append((combo, "COMBO_STRS", list(offered)))
                    other.append((combo, "SINGLE_SELIDX", getattr(item, chosen)))
            return [("OPEN", 7, [0], other)]
        u = self._item()
        self.dialog = {
            "ticks": set(),
            "power": None,
            "mode": None,
            "fan": u.fan_icon,
            "airflow": u.airflow,
        }
        parts = [
            (0, "LABEL", "Detailed Setup : " + u.name),
            (2, "LABEL", "On/Off"),
            (3, "ENABLED", 0),
            (3, "CHECK_STATE", 1 if u.running else 0),
            (4, "ENABLED", 0),
            (4, "CHECK_STATE", 0 if u.running else 1),
            (17, "LABEL", "Operation Mode"),
            (18, "ENABLED", 0),
            (18, "COMBO_STRS", list(u.modes)),
            (18, "SINGLE_SELIDX", u.modes.index(u.mode)),
            (28, "LABEL", "Fan Speed"),
            (28, "VISIBLED", 1 if u.fan_icon else 0),
            (31, "ICONID", u.fan_icon or 3011),
            (88, "VISIBLED", 1 if u.airflow else 0),
            (86, "VISIBLED", 1 if u.filter_sign else 0),
            (33, "VISIBLED", 0),  # ventilation mode/amount: ventilation units only
            (35, "VISIBLED", 0),
            (142, "VISIBLED", 1 if u.dust_alarm else 0),
            (143, "VISIBLED", 1 if u.dust_alarm else 0),
            (89, "ENABLED", 0),
            (90, "ENABLED", 0),
            (91, "ICONID", u.airflow or 3113),
            (27, "COMBO_STRS", ["On", "Off"]),
            (27, "SINGLE_SELIDX", 0 if u.overtime else 1),
            (63, "COMBO_STRS", ["Enable", "Disable"]),
            (63, "SINGLE_SELIDX", 0 if u.cool_limit[0] else 1),
            (65, "TEXT", f"{u.cool_limit[2]:.1f}"),
            (69, "TEXT", f"{u.cool_limit[1]:.1f}"),
            (73, "COMBO_STRS", ["Enable", "Disable"]),
            (73, "SINGLE_SELIDX", 0 if u.heat_limit[0] else 1),
            (75, "TEXT", f"{u.heat_limit[2]:.1f}"),
            (79, "TEXT", f"{u.heat_limit[1]:.1f}"),
        ]
        for radios, checked in (
            ((8, 9, 10), u.lock_start_stop),
            ((12, 13), min(u.lock_mode, 1)),
            ((15, 16), min(u.lock_setpoint, 1)),
        ):
            parts.extend(
                (p, "CHECK_STATE", 1 if i == checked else 0)
                for i, p in enumerate(radios)
            )
        return [("OPEN", 7, [0], parts + self._extra_parts(u))]

    @staticmethod
    def _extra_parts(u: FakeUnit) -> list[tuple[int, str, Any]]:
        """Return the tabs and optional settings (hidden, disabled if not offered)."""
        outputs = any(
            v is not None for v in (u.output_repeat, u.analog_output, u.multistate)
        )
        hw = any(
            v is not None
            for v in (
                u.low_noise,
                u.outdoor_low_noise,
                u.capacity,
                u.reheat,
                u.storage_water,
            )
        )
        offered = {
            37: u.analog_output is not None,
            94: u.output_repeat is not None,
            107: u.reheat is not None,
            111: u.leaving_water is not None,
            115: u.leaving_water is not None,
            119: u.low_noise is not None,
            123: u.storage_water is not None,
            129: u.multistate is not None,
            140: u.outdoor_low_noise is not None,
            147: u.capacity is not None,
        }
        flags = [1, 1, 1, 0, int(outputs), int(hw)]
        parts: list[tuple[int, str, Any]] = [
            (1, "TAB_NAME", SETTINGS_TABS),
            (1, "TAB_VISIBLE", [0, flags]),
            (144, "VISIBLED", int(u.r32_alarm_stop)),
            (144, "ENABLED", int(u.r32_alarm_stop)),
        ]
        for check, controls in EXTRA_CONTROLS.items():
            if offered[check]:
                parts.append((check, "VISIBLED", 1))
                parts.extend(_enabled(controls, 0))
            elif check in {37, 94, 129}:
                # Like a real iTM: on the hidden outputs tab, shown but disabled.
                parts.extend(_enabled((check, *controls), 0))
            else:
                parts.extend(
                    (p, t, 0)
                    for p in (check, *controls)
                    for t in ("VISIBLED", "ENABLED")
                )
        cool, heat = u.leaving_water or (10.0, 45.0)
        parts += [
            (38, "TEXT", f"{u.analog_output or 0:.4f}"),
            (96, "CHECK_STATE", 1 if u.output_repeat else 0),
            (97, "CHECK_STATE", 0 if u.output_repeat else 1),
            (98, "COMBO_NUMS", list(range(1, 11))),
            (98, "SINGLE_SELIDX", u.output_interval),
            (108, "CHECK_STATE", 1 if u.reheat else 0),
            (110, "CHECK_STATE", 0 if u.reheat else 1),
            (112, "TEXT", f"{cool:.1f}"),
            (116, "TEXT", f"{heat:.1f}"),
            (124, "TEXT", f"{u.storage_water or 0:.1f}"),
            (148, "COMBO_NUMS", CAPACITY_OPTIONS),
            (148, "SINGLE_SELIDX", u.capacity or 0),
        ]
        for combo, options, index in (
            (120, LOW_NOISE_OPTIONS, u.low_noise),
            (141, LOW_NOISE_OPTIONS, u.outdoor_low_noise),
            (130, MULTISTATE_OPTIONS, u.multistate),
        ):
            shown = options if index is not None else []
            parts += [
                (combo, "COMBO_STRS", shown),
                (combo, "SINGLE_SELIDX", index or 0),
            ]
        return parts

    def _extra_settings_event(self, ev: dict[str, Any]) -> list[Update] | None:
        """Handle the optional settings: checkboxes, controls and number inputs."""
        part, event, number = ev["part"], ev["event"], ev["number"]
        d = self.dialog
        if d is None:
            return None
        pending = d.setdefault("unit", {})
        if part in EXTRA_CONTROLS and event == 1:
            (d["ticks"].add if number else d["ticks"].discard)(part)
            enabled = _enabled(EXTRA_CONTROLS[part], number)
            return [("UPDATE", 7, [], [(part, "CHECK_STATE", number), *enabled])]
        if part in EXTRA_OWNER and event == 1:
            if EXTRA_OWNER[part] not in d["ticks"]:
                msg = f"part {part} clicked while disabled: the real iTM hangs"
                raise RuntimeError(msg)
            if part in EXTRA_NUMBERS:
                d["extra_number_for"] = part
                _, kind, low, high = EXTRA_NUMBERS[part]
                data = [70, 6, kind, "Setting", 20, 0, low, 0, high, 0, 2, 1]
                return [("DLG_OPEN", 7, [*data, *["x"] * 11], [])]
            if part in EXTRA_RADIOS:
                pending[EXTRA_RADIOS[part][0]] = EXTRA_RADIOS[part][1]
            else:
                pending[EXTRA_COMBOS[part]] = number
            return [("UPDATE", 7, [], [])]
        if part == 70 and "extra_number_for" in d:
            target = d.pop("extra_number_for")
            if event == 1:
                pending[EXTRA_NUMBERS[target][0]] = (
                    ev["array"][0] + ev["array"][1] / 1000
                )
            return [("DLG_CLOSE", 7, [70, 6], [])]
        return None

    def _unit_settings_event(self, ev: dict[str, Any]) -> list[Update] | None:  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        """Locks, overtime, limits and their number inputs in the settings dialog."""
        part, event, number = ev["part"], ev["event"], ev["number"]
        d = self.dialog
        if d is None:
            return None
        pending = d.setdefault("unit", {})
        radios = {
            8: ("start_stop", 0),
            9: ("start_stop", 1),
            10: ("start_stop", 2),
            12: ("mode", 0),
            13: ("mode", 1),
            15: ("setpoint", 0),
            16: ("setpoint", 1),
        }
        if part in {7, 11, 14, 26, 60} and event == 1:
            (d["ticks"].add if number else d["ticks"].discard)(part)
            return [("UPDATE", 7, [], [(part, "CHECK_STATE", number)])]
        if part in radios:
            needs = {"start_stop": 7, "mode": 11, "setpoint": 14}[radios[part][0]]
            if needs in d["ticks"]:
                pending["lock_" + radios[part][0]] = radios[part][1]
            return [("UPDATE", 7, [], [])]
        if part == 27 and 26 in d["ticks"]:
            pending["overtime"] = number == 0
            return [("UPDATE", 7, [], [])]
        if part in {63, 73} and 60 in d["ticks"]:
            pending["cool_on" if part == 63 else "heat_on"] = number == 0
            return [("UPDATE", 7, [], [])]
        if part in {67, 71, 77, 81}:
            d["number_for"] = part
            low, high = (16, 32)
            data = [70, 6, 2, "Setting", 20, 0, low, 0, high, 0, 2, 1, *["x"] * 11]
            return [("DLG_OPEN", 7, data, [])]
        if part == 70 and "number_for" in d:
            target = d.pop("number_for")
            if event == 1:
                value = ev["array"][0] + ev["array"][1] / 1000
                pending[
                    {67: "cool_max", 71: "cool_min", 77: "heat_max", 81: "heat_min"}[
                        target
                    ]
                ] = value
            return [("DLG_CLOSE", 7, [70, 6], [])]
        return None

    @staticmethod
    def _apply_unit_settings(u: FakeUnit, pending: dict[str, Any]) -> None:
        if "lock_start_stop" in pending:
            u.lock_start_stop = pending["lock_start_stop"]
        if "lock_mode" in pending:
            u.lock_mode = pending["lock_mode"] * 2  # the fake keeps 0 / 1 / 2
        if "lock_setpoint" in pending:
            u.lock_setpoint = pending["lock_setpoint"] * 2
        if "overtime" in pending:
            u.overtime = pending["overtime"]
        for key in ("cool", "heat"):
            enabled, low, high = getattr(u, f"{key}_limit")
            enabled = pending.get(f"{key}_on", enabled)
            low = pending.get(f"{key}_min", low)
            high = pending.get(f"{key}_max", high)
            setattr(u, f"{key}_limit", (enabled, low, high))
        for key in (
            "low_noise",
            "outdoor_low_noise",
            "capacity",
            "reheat",
            "storage_water",
            "output_repeat",
            "output_interval",
            "analog_output",
            "multistate",
        ):
            if key in pending:
                setattr(u, key, pending[key])
        if u.leaving_water is not None:
            cool, heat = u.leaving_water
            u.leaving_water = (
                pending.get("lw_cool", cool),
                pending.get("lw_heat", heat),
            )

    def _dialog_event(  # noqa: PLR0912  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        handled = self._unit_settings_event(ev) or self._extra_settings_event(ev)
        if handled is not None:
            return handled
        part, event, number = ev["part"], ev["event"], ev["number"]
        d, u = self.dialog, self._item()
        updates: list[Update] = [("UPDATE", 7, [], [])]
        if d is None:
            updates = []
        elif part in {33, 35, 86, 142, 143} and event == 1:  # option checkboxes
            (d["ticks"].add if number else d["ticks"].discard)(part)
            updates = [("UPDATE", 7, [], [(part, "CHECK_STATE", number)])]
        elif part in {34, 36} and part - 1 in d["ticks"]:
            d["vent_mode" if part == 34 else "vent_amount"] = number
        elif part == 88 and event == 1 and d.get("airflow"):
            (d["ticks"].add if number else d["ticks"].discard)(part)
            enabled = _enabled((89, 90), number)
            updates = [("UPDATE", 7, [], [(88, "CHECK_STATE", number), *enabled])]
        elif part in {89, 90} and event == 1:
            if 88 not in d["ticks"]:
                msg = "an airflow button clicked while disabled: the real iTM hangs"
                raise RuntimeError(msg)
            order = [3101, 3103, 3105, 3107, 3109, 3111]  # "down" steps forward
            step = 1 if part == 90 else -1
            d["airflow"] = order[(order.index(d["airflow"]) + step) % len(order)]
            updates = [("UPDATE", 7, [], [(91, "ICONID", d["airflow"])])]
        elif part in {2, 17, 28} and event == 1:  # checkboxes
            (d["ticks"].add if number else d["ticks"].discard)(part)
            updates = [("UPDATE", 7, [], [(part, "CHECK_STATE", number)])]
        elif part in {3, 4} and 2 in d["ticks"]:
            d["power"] = part == 3
        elif part == 18 and 17 in d["ticks"]:
            d["mode"] = u.modes[number]
        elif part == 29 and 28 in d["ticks"] and d["fan"]:
            order = [3009, 3011, 3013, 3015]
            d["fan"] = order[(order.index(d["fan"]) + 1) % 4]
            updates = [("UPDATE", 7, [], [(31, "ICONID", d["fan"])])]
        elif part == 5 and not d.get("confirming"):
            # OK asks for confirmation first, like the real iTM.
            d["confirming"] = True
            confirm = [87, 2, "Execute setting?", "Confirm", "Yes", "No"]
            updates = [("DLG_OPEN", 7, confirm, [])]
        elif part == 87 and d.get("confirming") and event == 2:  # No: back
            d["confirming"] = False
            updates = [("DLG_CLOSE", 7, [87, 2], [])]
        elif (part == 87 and d.get("confirming") and event == 1) or part == 6:
            # Yes applies the settings, cancel (6) drops them.
            if part == 87:
                pending = dict(d.get("unit", {}))
                for target in self._operated_units():
                    self._later(self._dialog_change(target, dict(d)))
                    if isinstance(target, FakeUnit):  # locks, limits, overtime
                        self._later(
                            functools.partial(
                                self._apply_unit_settings, target, pending
                            )
                        )
            self.dialog = None
            updates = [("CLOSE", 7, [], []), ("UPDATE", 4, [], self._panel_delta())]
        return updates

    @staticmethod
    def _dialog_change(u: FakeUnit, d: dict[str, Any]) -> Callable[[], None]:
        def change() -> None:
            if d["power"] is not None:
                u.running = d["power"]
                u.setback = 0  # start or stop ends setback
            if d["mode"] is not None:
                u.mode = d["mode"]
            if 28 in d["ticks"] and d["fan"]:
                u.fan_icon = d["fan"]
            if 88 in d["ticks"] and d.get("airflow"):
                u.airflow = d["airflow"]
            if 86 in d["ticks"]:
                u.filter_sign = False
            if d["ticks"] & {142, 143}:
                u.dust_alarm = False
            if 33 in d["ticks"]:
                setattr(u, "vent_mode", d["vent_mode"])  # noqa: B010 - a ventilation unit
            if 35 in d["ticks"]:
                setattr(u, "vent_amount", d["vent_amount"])  # noqa: B010

        return change

    def _later(self, change: Callable[[], None]) -> None:
        if self.latency_logins:
            self._pending.append([self.latency_logins, change, self._item()])
        else:
            change()

    def _check_overlap(self) -> None:
        if any(p[2] is self._item() for p in self._pending):
            self.overlaps += 1

    # -- model -> parts -------------------------------------------------------------
    def _item(self) -> FakeUnit:
        """Return the selected unit (for a selected zone: its first indoor unit)."""
        item = self.path[-1].children[self.selected]
        if isinstance(item, FakeZone):
            return self._operated_units()[0]
        return cast("FakeUnit", item)

    def _operated_units(self) -> list[FakeUnit]:
        """Return the units a change hits: the selected unit, or a zone's units."""
        item = self.path[-1].children[self.selected]
        if isinstance(item, FakeZone):
            return [c for c in item.children if isinstance(c, FakeUnit)]
        return [cast("FakeUnit", item)]

    def _icons(self) -> dict[str, Any]:
        icons = []
        for c in self.path[-1].children:
            if isinstance(c, FakeZone):
                icons.append([c.name, 1000, 0])
            elif isinstance(c, FakeVentilation):
                icons.append([c.name, 2201, 0 if c.running else 2])
            elif isinstance(c, FakeAnalog):
                icons.append([c.name, 2601, 5 if c.alarm else 2])
            else:
                icons.append([c.name, 2114, 0 if c.running else 2])
        return {"icons": icons, "selected": self.selected}

    def _panel_delta(self) -> list[tuple[int, str, Any]]:
        children = self.path[-1].children
        item = children[self.selected] if children else None
        full: dict[tuple[int, str], Any]
        if isinstance(item, FakeUnit):
            full = {
                (0, "LABEL"): item.name,
                (1, "ICONID_MODE"): [
                    2114,
                    0 if item.running else 11 if item.setback else 2,
                ],
                (4, "LABEL"): f"{item.temp:.1f}°C",
                (5, "LABEL"): item.mode,
                (9, "COMBO_STRS"): POWER_OPTIONS,
                (9, "SINGLE_SELIDX"): 0 if item.running else item.setback or 3,
                (11, "COMBO_NUMS"): list(item.setpoint_range),
                (11, "SINGLE_SELIDX"): item.setpoint_range.index(item.setpoint),
                (14, "ICONID"): item.fan_icon or 3011,
                (2, "VISIBLED"): 0,
                (19, "VISIBLED"): 0,
                (4, "VISIBLED"): 1,
                (5, "VISIBLED"): 1,
            }
        elif isinstance(item, FakeZone) and self._operated_units():
            first = self._operated_units()[0]
            running = any(u.running for u in self._operated_units())
            full = {
                (0, "LABEL"): item.name,
                (1, "ICONID_MODE"): [1000, 0 if running else 2],
                (9, "COMBO_STRS"): POWER_OPTIONS,
                (9, "SINGLE_SELIDX"): 0 if running else first.setback or 3,
                (11, "COMBO_NUMS"): list(first.setpoint_range),
                (11, "SINGLE_SELIDX"): first.setpoint_range.index(first.setpoint),
                (14, "ICONID"): first.fan_icon or 3011,
                (4, "VISIBLED"): 0,
                (5, "VISIBLED"): 0,
            }
        elif isinstance(item, FakeVentilation):
            full = {
                (0, "LABEL"): item.name,
                (1, "ICONID_MODE"): [2201, 0 if item.running else 2],
                (9, "COMBO_STRS"): ["Start", "Stop"],
                (9, "SINGLE_SELIDX"): 0 if item.running else 1,
                (11, "COMBO_NUMS"): [],
                (14, "ICONID"): 3007,
                (4, "VISIBLED"): 0,
                (5, "VISIBLED"): 0,
            }
        elif isinstance(item, FakeAnalog):
            full = {
                (0, "LABEL"): item.name,
                (1, "ICONID_MODE"): [2601, 5 if item.alarm else 2],
                (9, "COMBO_STRS"): POWER_OPTIONS,
                (9, "SINGLE_SELIDX"): -1,
                (4, "VISIBLED"): 0,
                (5, "VISIBLED"): 0,
            }
        else:
            full = {
                (0, "LABEL"): item.name if item else "",
                (4, "VISIBLED"): 0,
                (5, "VISIBLED"): 0,
            }
        delta = [(p, t, v) for (p, t), v in full.items() if self.shown.get((p, t)) != v]
        self.shown.update(full)
        return delta
