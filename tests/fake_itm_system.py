"""The simulated iTM's system settings and service screens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .fake_itm_base import (
    DST_HOURS,
    HARDWARE_OPTIONS,
    MONTHS,
    SCREENSAVER_MODES,
    SERVICE_BUTTONS,
    WEEKDAYS,
    WEEKS,
    FakeITMBase,
    Update,
    _enabled,
    _rows,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class SystemMixin(FakeITMBase):
    """The system settings and service screens."""

    def _maintenance(self, *, on: bool) -> list[str]:
        """Return the unit names in (or not in) maintenance, in list order."""
        return [u.name for u in self.units if (u.name in self.in_maintenance) == on]

    def _service_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update] | None:
        """Handle the menu's corner login, its password dialog and service buttons."""
        part, event = ev["part"], ev["event"]
        if part in {87, 88, 89, 90}:
            self.corners = [*self.corners, part][-4:]
            if self.corners != [87, 88, 89, 90]:
                return [("UPDATE", 5, [], [])]
            data = [93, 5, "Service password", 1, 15, 4]
            data += ["OK", "Cancel", "", "", "", "Error", 0]
            return [("DLG_OPEN", 5, data, [])]
        if part == 93:
            self.corners = []
            close: list[Update] = [("DLG_CLOSE", 5, [93, 5], [])]
            if event != 1:
                return close
            if ev["string"] != self.service_password:
                error = [94, 1, "Password incorrect", "Error", "OK"]
                return [*close, ("DLG_OPEN", 5, error, [])]
            self.service_mode = True
            shown = [(p, "VISIBLED", 1) for p in (95, 96, 97, 98, 99)]
            return [*close, ("UPDATE", 5, [], shown)]
        if part not in SERVICE_BUTTONS:
            return None
        if not self.service_mode:
            info = [
                92,
                3,
                "Function used by another user.",
                "Information",
                "Close",
            ]
            return [("DLG_OPEN", 5, info, [])]
        target = SERVICE_BUTTONS[part]
        self.open_screens.add(target)
        return [("OPEN", target, [-1], self._service_parts(target))]

    def _service_parts(self, target: int) -> list[tuple[int, str, Any]]:
        """Return a service screen's parts, with generic values."""

        def rows(
            *cells: list[str], selected: list[int] | None = None
        ) -> dict[str, Any]:
            return {**_rows([list(c) for c in cells]), "selected": selected or []}

        units = [u.name for u in self.units]
        zones = ["GMT+00:00:Zone A", "GMT+01:00:Zone B", "GMT+02:00:Zone C"]
        parts: dict[int, list[tuple[int, str, Any]]] = {
            1500: [(2, "LIST_ROW", rows(*[[z] for z in zones], selected=[1]))],
            1505: [
                (
                    1,
                    "LIST_ROW",
                    rows(
                        *[[n, n, "0", "Able", "", "", "Indoor"] for n in units],
                        ["9:4-15", "", "", "", "", "", ""],  # a free address
                    ),
                )
            ],
            1509: [(2, "TEXT", "Installer"), (5, "TEXT", ""), (8, "TEXT", "")],
            1516: [(2, "TEXT", ""), (6, "TEXT", "DO-1"), (15, "CHECK_STATE", 1)],
            1507: [(2, "CHECK_STATE", 1), (6, "CHECK_STATE", 1)],
            1501: [(2, "LIST_ROW", rows(["Option", "KEY-1"])), (3, "TEXT", "x")],
            1502: [
                (2, "LIST_ROW", rows(["Initialising"])),
                (4, "LIST_ROW", rows(["Emergency Stop"], ["E-mail Error Report"])),
            ],
            1511: [(4, "CHECK_STATE", 1)],
        }
        return parts.get(target, [])

    def _dst_parts(self) -> list[tuple[int, str, Any]]:
        options = {
            9: MONTHS,
            10: WEEKS,
            11: WEEKDAYS,
            14: MONTHS,
            15: WEEKS,
            16: WEEKDAYS,
            **DST_HOURS,
        }
        parts: list[tuple[int, str, Any]] = []
        for part, opts in options.items():
            parts += [
                (part, "COMBO_STRS", opts),
                (part, "SINGLE_SELIDX", self.dst_rule[part]),
                (part, "ENABLED", 1 if self.dst else 0),
            ]
        return parts

    def _touch_panel_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        """Handle picks, the delay input and OK (with its confirm) on 2200 and 2000."""
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        ok = {2200: 12, 2000: 10}[screen]
        if part == ok and event == 1:
            self.system_confirming = screen
            confirm = [30, 2, "Execute setting?", "Confirm", "Yes", "No"]
            return [("DLG_OPEN", screen, confirm, [])]
        if part == 30 and self.system_confirming == screen:
            self.system_confirming = None
            if event != 1:
                return [("DLG_CLOSE", screen, [30, 2], [])]
            for key, value in self.pending_system.items():
                if screen == 2200:
                    self.hardware[key] = value
                else:
                    self.screensaver[key] = value
            self.pending_system = {}
            self.open_screens.discard(screen)
            return [("DLG_CLOSE", screen, [30, 2], []), ("CLOSE", screen, [], [])]
        if screen == 2200 and part in HARDWARE_OPTIONS and event == 1:
            self.pending_system[part] = ev["number"]
            return [("UPDATE", 2200, [], [])]
        if screen == 2000 and part == 2 and event == 1:
            self.pending_system["mode"] = ev["number"]
            return [("UPDATE", 2000, [], [])]
        if screen == 2000 and part in {8, 9} and event == 1:
            self.pending_system["off_on_error"] = part == 8
            return [("UPDATE", 2000, [], [])]
        if screen == 2000 and part == 6:
            data = [70, 6, 0, "Start time", 30, 0, 1, 0, 60, 0, 2, 1, *["x"] * 11]
            return [("DLG_OPEN", 2000, data, [])]
        if screen == 2000 and part == 70:
            if event == 1:
                self.pending_system["delay"] = ev["number"]
            return [("DLG_CLOSE", 2000, [70, 6], [])]
        return []

    def _system_parts(  # pylint: disable=too-many-return-statements
        self, target: int
    ) -> list[tuple[int, str, Any]]:
        """Return the parts of a system screen, shaped like the real iTM's."""

        def rows(*cells: list[str], icons: int | None = None) -> dict[str, Any]:
            return {
                "rows": [
                    {
                        "text": c,
                        "a": [icons if i == 2 and icons else -1 for i in range(len(c))],
                        "b": [-1] * len(c),
                    }
                    for c in cells
                ],
                "selected": [],
            }

        units = [u.name for u in self.units]
        parts: dict[int, list[tuple[int, str, Any]]] = {
            1600: [
                (2, "TEXT", "Top"),
                (
                    4,
                    "LIST_ROW",
                    rows(
                        ["Top", "-", "1", "0"],
                        *[
                            [z["name"], "1", "0", str(len(units))]
                            for z in self.zone_attrs.values()
                        ],
                    ),
                ),
                (9, "ENABLED", 0),
            ],
            1700: [
                (
                    1,
                    "LIST_ROW",
                    rows(
                        *[["Indoor", n, "", i] for n, i in self.point_info.items()],
                        icons=2114,
                    ),
                ),
                (2, "ENABLED", 0),
            ],
            2700: [
                (
                    2,
                    "LIST_ROW",
                    rows(*[["Indoor", n, "On"] for n in self._maintenance(on=True)]),
                ),
                (
                    8,
                    "LIST_ROW",
                    rows(*[["Indoor", n, "Off"] for n in self._maintenance(on=False)]),
                ),
            ],
            1800: [
                (p, "TEXT", v)
                for p, v in zip((8, 9, 10, 11, 12, 13, 14), self.network, strict=True)
            ],
            1300: [
                (1, "LIST_ROW", rows(["Enable", "ops@example.com"])),
                *_enabled((3, 4, 7, 8), 0),
            ],
            1200: [
                (2, "LIST_ROW", rows(*self.web_users)),
                (5, "ENABLED", 0),
                (6, "ENABLED", 0),
            ],
            2000: [
                (2, "COMBO_STRS", SCREENSAVER_MODES),
                (2, "SINGLE_SELIDX", self.screensaver["mode"]),
                (5, "TEXT", str(self.screensaver["delay"])),
                (8, "CHECK_STATE", 1 if self.screensaver["off_on_error"] else 0),
                (9, "CHECK_STATE", 0 if self.screensaver["off_on_error"] else 1),
            ],
            2200: [
                item
                for part, options in HARDWARE_OPTIONS.items()
                for item in (
                    (part, "COMBO_STRS", options),
                    (part, "SINGLE_SELIDX", self.hardware[part]),
                )
            ],
            2100: [(3, "CHECK_STATE", 1)],
            1100: [(2, "TEXT", "Connect"), (4, "TEXT", "198.51.100.7")],
            2900: [
                (2, "CHECK_STATE", 0),
                (5, "TEXT", "20/11/2024"),
                (8, "TEXT", "01/01/2025"),
                (13, "TEXT", "20"),
                (20, "TEXT", ""),
                (21, "LIST_ROW", rows([units[0], "12.5", "0,3"], [units[1], "--", ""])),
            ],
            3210: [
                (2, "TEXT", "20/12/2024"),
                (5, "TEXT", "02/01/2025"),
                (8, "TEXT", "Top&gt;All"),
            ],
            3219: [
                (3, "COMBO_STRS", ["01/2025-01/2025", "01/2024-12/2024"]),
                (3, "SINGLE_SELIDX", 0),
            ],
        }
        return parts[target]


class SystemWrites:
    """One event on a system write screen of the fake.

    Maintenance (2700), point settings (1700/1701), zone settings (1600/1603), web
    users (1200/1204), language (2500/2501) and the time screen (2400). Every OK asks
    "Execute setting?" (dialog 30); only Yes applies.
    """

    def __init__(self, itm: SystemMixin, ev: dict[str, Any]) -> None:
        """Take the event and the fake it is for."""
        self.itm = itm
        self.ev = ev
        self.screen, self.part, self.event = ev["screen"], ev["part"], ev["event"]
        self.pending = itm.pending_system
        self.dialog = itm.system_dialog

    def handle(self) -> list[Update]:
        """Return the reply's updates for the event."""
        if self.part == 30 and self.dialog.get("screen") == self.screen:
            return self._answer_confirm()
        if self.ev["dtype"] == 8:  # a list selection
            return self._select()
        handler = {
            2700: self._maintenance,
            1700: self._points,
            1701: self._point_settings,
            1600: self._zones,
            1603: self._zone_settings,
            1200: self._users,
            1204: self._user_setup,
            1202: self._user_zones,
            2500: self._region,
            2501: self._language,
            2400: self._time,
            1800: self._network,
        }[self.screen]
        return handler() or [("UPDATE", self.screen, [], [])]

    # -- shared ---------------------------------------------------------------
    def _confirm(
        self, apply: Callable[[], None], *, close: bool = True, text: str = ""
    ) -> list[Update]:
        self.itm.system_dialog = {
            "screen": self.screen,
            "apply": apply,
            "close": close,
        }
        confirm = [30, 2, text or "Execute setting?", "Confirm", "Yes", "No"]
        return [("DLG_OPEN", self.screen, confirm, [])]

    def _answer_confirm(self) -> list[Update]:
        d, self.itm.system_dialog = self.dialog, {}
        updates: list[Update] = [("DLG_CLOSE", self.screen, [30, 2], [])]
        if self.event != 1:
            return updates
        self.itm.pending_system = {}  # before apply: it may keep something for later
        d["apply"]()
        if d.get("logout"):
            self.itm.session = None
            return [("LOGOUT", -1, [3], [])]
        if d["close"]:
            self.itm.open_screens.discard(self.screen)
            updates.append(("CLOSE", self.screen, [], []))
        return updates

    def _select(self) -> list[Update]:
        if self.screen == 1202:
            self.itm.system_selected[1202] = list(self.ev["array"])
            return [("UPDATE", 1202, [], [])]
        self.itm.system_selected[self.screen] = self.ev["array"][0]
        enable = {1700: [2], 1600: [9], 1200: [5, 6], 2700: [4, 5]}.get(self.screen, [])
        return [("UPDATE", self.screen, [], _enabled(enable, 1))]

    def _selected(self) -> int:
        """Return the selected row; a button clicked without one hangs the real iTM."""
        if self.screen not in self.itm.system_selected:
            msg = f"({self.screen}, {self.part}) clicked while disabled"
            raise RuntimeError(msg)
        return int(self.itm.system_selected[self.screen])

    def _text_dialog(
        self, key: str, dialog_id: int, current: str, low: int, high: int
    ) -> list[Update]:
        self.dialog.update(text_for=key, text_dialog=dialog_id)
        data = [dialog_id, 4, "Label", current, low, high, 0, 0, 1, "Words"]
        data += ["OK", "Cancel", *[""] * 11, ""]
        return [("DLG_OPEN", self.screen, data, [])]

    def _text_answer(self) -> list[Update] | None:
        if self.part != self.dialog.get("text_dialog"):
            return None
        key = self.dialog.pop("text_for")
        self.dialog.pop("text_dialog")
        if self.event == 1:
            self.pending[key] = self.ev["string"]
        return [("DLG_CLOSE", self.screen, [self.part, 4], [])]

    # -- screens --------------------------------------------------------------
    def _maintenance(self) -> list[Update] | None:
        itm = self.itm
        if self.part in {4, 5}:
            listed = itm._maintenance(on=self.part == 5)  # pylint: disable=protected-access
            self.pending[listed[self._selected()]] = self.part == 4
            return None
        if self.part == 9:
            changes = dict(self.pending)

            def apply() -> None:
                for name, on in changes.items():
                    if on:
                        itm.in_maintenance.add(name)
                    else:
                        itm.in_maintenance.discard(name)

            return self._confirm(apply)
        return None

    def _points(self) -> list[Update] | None:
        if self.part != 2:
            return None
        name = list(self.itm.point_info)[self._selected()]
        self.pending["point"] = name
        self.itm.open_screens.add(1701)
        parts = [
            (2, "TEXT", "Indoor"),
            (4, "TEXT", name),
            (7, "TEXT", self.itm.point_info[name]),
        ]
        return [("OPEN", 1701, [-1], parts)]

    def _point_settings(self) -> list[Update] | None:
        itm, point = self.itm, self.pending["point"]
        if self.part == 5:
            return self._text_dialog("name", 14, point, 1, 12)
        if self.part == 8:
            return self._text_dialog("info", 16, itm.point_info[point], 0, 50)
        if self.part == 12:
            changes = dict(self.pending)

            def apply() -> None:
                info = changes.get("info", itm.point_info[point])
                del itm.point_info[point]
                itm.point_info[changes.get("name", point)] = info

            return self._confirm(apply)
        return self._text_answer()

    def _zones(self) -> list[Update] | None:
        if self.part != 9:
            return None
        key = list(self.itm.zone_attrs)[self._selected() - 1]  # row 0: the position
        z = self.itm.zone_attrs[key]
        self.pending["zone"] = key
        self.itm.open_screens.add(1603)
        parts = [
            (2, "TEXT", z["name"]),
            (5, "TEXT", z["info"]),
            (8, "TEXT", str(z["start"])),
            (12, "TEXT", str(z["stop"])),
        ]
        return [("OPEN", 1603, [-1], parts)]

    def _zone_settings(self) -> list[Update] | None:
        z = self.itm.zone_attrs[self.pending["zone"]]
        if self.part in {3, 6}:
            key = "name" if self.part == 3 else "info"
            low, high = (1, 12) if self.part == 3 else (0, 50)
            return self._text_dialog(
                key, 20 if self.part == 3 else 22, z[key], low, high
            )
        if self.part in {10, 14}:
            self.dialog["number_for"] = "start" if self.part == 10 else "stop"
            dialog_id = 23 if self.part == 10 else 24
            data = [dialog_id, 6, 0, "Interval", 0, 0, 0, 0, 180, 0, 4, 0, *["x"] * 11]
            return [("DLG_OPEN", 1603, data, [])]
        if self.part in {23, 24} and "number_for" in self.dialog:
            key = self.dialog.pop("number_for")
            if self.event == 1:
                self.pending[key] = self.ev["number"]
            return [("DLG_CLOSE", 1603, [self.part, 6], [])]
        if self.part == 18:
            changes = {k: v for k, v in self.pending.items() if k in z}
            return self._confirm(lambda: z.update(changes))
        return self._text_answer()

    def _users(self) -> list[Update] | None:
        itm = self.itm
        if self.part == 4:
            name, kind = "New User01", "User"
        elif self.part == 5:
            name, kind = itm.web_users[self._selected()]
        elif self.part == 6:
            row = self._selected()

            def delete() -> None:
                del itm.web_users[row]

            return self._confirm(delete, close=False)
        else:
            return None
        admin = kind == "Manager"
        self.pending.update(user=name, new=self.part == 4, name=name, admin=admin)
        itm.open_screens.add(1204)
        parts = [
            (2, "TEXT", name),
            (8, "CHECK_STATE", int(admin)),
            (9, "CHECK_STATE", int(not admin)),
        ]
        return [("OPEN", 1204, [-1], parts)]

    def _user_setup(self) -> list[Update] | None:  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        itm, pending = self.itm, self.pending
        if self.part == 3:
            return self._text_dialog("name", 19, pending["name"], 1, 15)
        if self.part == 6:
            self.dialog["password"] = True
            data = [20, 5, "Enter the new password.", 4, 15, 4]
            data += ["OK", "Cancel", "", "", "", "Error", 0]
            return [("DLG_OPEN", 1204, data, [])]
        if self.part == 20 and self.dialog.pop("password", False):
            if self.event == 1:
                pending["password"] = self.ev["string"]
            return [("DLG_CLOSE", 1204, [20, 5], [])]
        if self.part in {8, 9}:
            pending["admin"] = self.part == 8
            return None
        if self.part == 12:
            itm.open_screens.add(1202)
            zones = [[z["name"]] for z in itm.zone_attrs.values()]
            return [("OPEN", 1202, [-1], [(2, "LIST_ROW", _rows(zones))])]
        if self.part == 17 and pending["new"] and not pending.get("zones"):
            error = [31, 1, "Managed area not registered", "Error", "OK"]
            return [("DLG_OPEN", 1204, error, [])]
        if self.part == 17:
            changes = dict(pending)

            def apply() -> None:
                kind = "Manager" if changes["admin"] else "User"
                if changes["new"]:
                    itm.web_users.append([changes["name"], kind])
                else:
                    row = next(r for r in itm.web_users if r[0] == changes["user"])
                    row[1] = kind
                if "password" in changes:
                    itm.passwords[changes["name"]] = changes["password"]

            return self._confirm(apply)
        return self._text_answer()

    def _user_zones(self) -> list[Update] | None:
        if self.part == 3:
            self.pending["zones"] = self.itm.system_selected.get(1202)
            self.itm.open_screens.discard(1202)
            return [("CLOSE", 1202, [], [])]
        return None

    def _network(self) -> list[Update] | None:
        itm = self.itm
        if 15 <= self.part <= 21:
            index = self.part - 15
            self.dialog["network"] = index
            if index < 2:
                self.dialog.pop("network")
                return self._text_dialog(
                    f"net{index}", 25 + index, itm.network[index], 1, 30
                )
            data = [27, 7, "IP Address", itm.network[index], "a", "b", "c", "d"]
            return [("DLG_OPEN", 1800, data, [])]
        if self.part == 27 and "network" in self.dialog:
            index = self.dialog.pop("network")
            if self.event == 1:
                self.pending[f"net{index}"] = self.ev["string"]
            return [("DLG_CLOSE", 1800, [27, 7], [])]
        if self.part == 23:
            changes = dict(self.pending)

            def apply() -> None:
                for key, value in changes.items():
                    itm.network[int(key[3:])] = value

            return self._confirm(apply)
        return self._text_answer()

    def _region(self) -> list[Update] | None:
        itm = self.itm
        if self.part == 3:
            itm.open_screens.add(2501)
            radios = [
                (r, "CHECK_STATE", int(r == itm.language_radio)) for r in range(2, 12)
            ]
            return [("OPEN", 2501, [-1], radios)]
        formats = {
            7: ("time_24h", True),
            8: ("time_24h", False),
            10: ("fahrenheit", False),
            11: ("fahrenheit", True),
            13: ("decimal_comma", False),
            14: ("decimal_comma", True),
        }
        if self.part in formats:
            key, value = formats[self.part]
            self.pending[key] = value
            return None
        if self.part == 5 and self.event == 1:
            self.pending["date_format"] = self.ev["number"]
            return None
        if self.part == 18:
            radio = self.pending.get("radio")
            picked = {k: v for k, v in self.pending.items() if k != "radio"}

            def apply() -> None:
                if radio is not None:
                    itm.language_radio = radio
                for key, value in picked.items():
                    setattr(itm, key, value)

            reply = self._confirm(apply)
            # Like the real iTM: the user's language changed, so its session ends.
            itm.system_dialog["logout"] = radio is not None
            return reply
        return None

    def _language(self) -> list[Update] | None:
        if 2 <= self.part <= 11:
            self.pending["radio"] = self.part
            return None
        if self.part == 12:
            # Only the region screen's OK applies; keep the pick for it.
            radio = self.pending.get("radio")

            def keep() -> None:
                if radio is not None:
                    self.itm.pending_system["radio"] = radio

            return self._confirm(keep)
        return None

    def _time(self) -> list[Update] | None:
        itm, pending = self.itm, self.pending
        if self.part in {6, 7}:
            pending["dst"] = self.part == 6
            on = int(self.part == 6)
            return [("UPDATE", 2400, [], _enabled(itm.dst_rule, on))]
        if self.part in itm.dst_rule:
            if not pending.get("dst", itm.dst):
                msg = "a disabled summer time combo was clicked"
                raise RuntimeError(msg)
            pending.setdefault("rule", {})[self.part] = self.ev["number"]
            return None
        if self.part == 3:
            self.dialog["time"] = True
            data = [20, 8, 7, "Date/Time", 2025, 0, 1, 10, 15, 30]
            data += [2010, 0, 1, 0, 0, 0, 2035, 11, 31, 23, 59, 59, *["x"] * 13]
            return [("DLG_OPEN", 2400, data, [])]
        if self.part == 20 and self.dialog.pop("time", False):
            if self.event == 1:
                pending["clock"] = list(self.ev["array"])
            return [("DLG_CLOSE", 2400, [20, 8], [])]
        if self.part == 18:
            changes = dict(pending)

            def apply() -> None:
                if "dst" in changes:
                    itm.dst = changes["dst"]
                itm.dst_rule.update(changes.get("rule", {}))
                if "clock" in changes:
                    y, m, day, hh, mm, ss = changes["clock"]
                    itm.clock_text = (
                        f"{day:02d}/{m + 1:02d}/{y} {hh:02d}:{mm:02d}:{ss:02d}"
                    )

            return self._confirm(apply)
        return None
