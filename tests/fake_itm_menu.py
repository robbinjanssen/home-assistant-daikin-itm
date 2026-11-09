"""The simulated iTM's main menu and its screens."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .fake_itm_base import (
    HISTORY_COLUMNS,
    MENU_CLOSE_PARTS,
    MENU_SCREENS,
    OPTIONAL_BUTTONS,
    SYSTEM_BUTTONS,
    SYSTEM_WRITE_SCREENS,
    FakeGroup,
    Update,
)
from .fake_itm_schedule import ScheduleMixin
from .fake_itm_system import SystemMixin, SystemWrites

HISTORY_DATE_ID = 20


class MenuMixin(ScheduleMixin, SystemMixin):
    """The main menu bar, the main menu and its screens."""

    def _menu_bar(self) -> list[tuple[int, str, Any]]:
        shown = 1 if self.errors else 0
        return [
            (6, "LABEL", "Tue, 31/12\n09:00"),
            (16, "VISIBLED", 1 if self.energy_saving else 0),
            (18, "VISIBLED", shown),
            (19, "VISIBLED", shown),
            (20, "VISIBLED", 1 if self.emergency_stop_active else 0),
        ]

    # -- main menu ------------------------------------------------------------------
    def _menu(  # noqa: C901, PLR0911, PLR0912  # pylint: disable=too-many-return-statements,too-many-branches
        self, ev: dict[str, Any]
    ) -> list[Update]:
        """Route an event of the main menu bar, the menu list or a menu screen."""
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if screen == 0 and part == 0:
            self.menu_open = True
            tabs = ["Automatic Ctrl.", "System Settings", "Operation Mgmt."]
            return [
                (
                    "OPEN",
                    5,
                    [0],
                    [(1, "TAB_NAME", tabs), (1, "TAB_VISIBLE", [0, [1, 1, 1]])],
                )
            ]
        if screen == 0 and part == 25:
            self.menu_open = False
            self.open_screens.clear()
            return [("CLOSE", 5, [], [])]
        if screen == 5 and self.menu_open:
            if part in OPTIONAL_BUTTONS:
                target = OPTIONAL_BUTTONS[part]
                if not self.optional_available[target]:
                    return []
                self.open_screens.add(target)
                return [("OPEN", target, [-1], self._optional_parts(target))]
            if part in MENU_SCREENS:
                target = MENU_SCREENS[part]
                self.open_screens.add(target)
                return self._open_menu_screen(target)
            service = self._service_event(ev)
            if service is not None:
                return service
            if part in SYSTEM_BUTTONS:
                target = SYSTEM_BUTTONS[part]
                if target in self.system_unavailable:
                    return []
                self.open_screens.add(target)
                return [("OPEN", target, [-1], self._system_parts(target))]
            return []
        if (
            screen in self.open_screens
            and part == MENU_CLOSE_PARTS.get(screen)
            and event == 1
        ):
            self.open_screens.discard(screen)
            if screen == 301:
                self.pending_members = None
            if screen == 302:
                self.pending_settings = {}
            if screen in {2000, 2200}:
                self.pending_system = {}
            return [("CLOSE", screen, [], [])]
        if screen in {2000, 2200} and screen in self.open_screens:
            return self._touch_panel_event(ev)
        if screen in SYSTEM_WRITE_SCREENS and screen in self.open_screens:
            return SystemWrites(self, ev).handle()
        if screen == 300:
            handled = self._changeover_event(ev)
            if handled is not None:
                return handled
        if (
            screen == 301
            and part in {2, 11, 7, 8, 12}
            and self.selected_group is not None
        ):
            return self._members_event(ev)
        if screen == 302 and self.selected_group is not None:
            return self._group_settings_event(ev)
        if screen == 300 and part in {9, 10} and self.selected_group is not None:
            g = self.groups[self.selected_group]
            target = 301 if part == 9 else 302
            self.open_screens.add(target)
            return [("OPEN", target, [-1], self._group_parts(g, target))]
        if screen in {3000, 3001, 700} and event == 1:
            handled = self._settings_write(ev)
            if handled is not None:
                return handled
        if screen == 3000 and part == 19:
            self.open_screens.add(3001)
            parts = [
                (4, "COMBO_NUMS", [1, 2, 3, 4, 5, 6]),
                (4, "SINGLE_SELIDX", self.recovery["cool"] - 1),
                (8, "COMBO_NUMS", [1, 2, 3, 4, 5, 6]),
                (8, "SINGLE_SELIDX", self.recovery["heat"] - 1),
            ]
            return [("OPEN", 3001, [-1], parts)]
        if screen == 800:
            return self._history_event(ev)
        if screen in {100, 102, 104, 107, 109}:
            return self._schedule_event(ev)
        return []

    def _changeover_event(self, ev: dict[str, Any]) -> list[Update] | None:  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        part, event = ev["part"], ev["event"]
        if part == 2 and event == 1:
            if not ev["array"]:  # a plain number is not a list selection
                return [("UPDATE", 300, [], [])]
            self.selected_group = ev["array"][0]
            g = self.groups[self.selected_group]
            return [("UPDATE", 300, [], [(8, "ENABLED", 0 if g.enabled else 1)])]
        if part in {4, 5} and self.selected_group is not None:
            g = self.groups[self.selected_group]
            if part == 4 and not g.members:
                return [("DLG_OPEN", 300, [40, 1, "No members", "Error", "OK"], [])]
            g.enabled = part == 4
            cell = [[self.selected_group, 0, "Enable" if g.enabled else "", -1, -1]]
            return [
                (
                    "UPDATE",
                    300,
                    [],
                    [(2, "LIST_CELL", cell), (8, "ENABLED", 0 if g.enabled else 1)],
                )
            ]
        if part == 7 and event == 1:
            self.naming = True
            data = [
                11,
                4,
                "Group Name",
                "New Group",
                1,
                32,
                0,
                0,
                1,
                "",
                "OK",
                "Cancel",
                *[""] * 12,
                "",
            ]
            return [("DLG_OPEN", 300, data, [])]
        if part == 11 and getattr(self, "naming", False):
            self.naming = False
            if event == 2:  # Cancel
                return [("DLG_CLOSE", 300, [11, 4], [])]
            self.groups.append(FakeGroup(ev["string"]))
            self.selected_group = len(self.groups) - 1
            return [
                ("UPDATE", 300, [], self._open_menu_screen(300)[0][3]),
                ("DLG_CLOSE", 300, [11, 4], []),
            ]
        if part == 8 and self.selected_group is not None:
            if self.groups[self.selected_group].enabled:
                return []  # the button is disabled: the real iTM would hang
            self.deleting = True
            return [
                (
                    "DLG_OPEN",
                    300,
                    [12, 2, "Delete?", "Confirm", "Yes", "No"],
                    [],
                )
            ]
        if part == 12 and getattr(self, "deleting", False):
            self.deleting = False
            if event == 1 and self.selected_group is not None:
                del self.groups[self.selected_group]
                self.selected_group = None
                return [
                    ("DLG_CLOSE", 300, [12, 2], []),
                    ("UPDATE", 300, [], self._open_menu_screen(300)[0][3]),
                ]
            return [("DLG_CLOSE", 300, [12, 2], [])]
        return None

    def _available(self, members: list[str]) -> list[tuple[str, str]]:
        """Return the points a group could hold: (name, the group they are in)."""
        out = []
        for u in self.units:
            if u.name in members:
                continue
            other = next((g.name for g in self.groups if u.name in g.members), "")
            out.append((u.name, other))
        return out

    def _members_event(self, ev: dict[str, Any]) -> list[Update]:
        part, number = ev["part"], ev["number"]
        g = self.groups[self.selected_group or 0]
        if self.pending_members is None:
            self.pending_members = list(g.members)
        pending = self.pending_members
        available = self._available(pending)
        if part in {2, 11}:
            self.member_pick = (part, ev["array"][0] if ev["array"] else number)
            return [("UPDATE", 301, [], [])]
        if part == 7 and self.member_pick and self.member_pick[0] == 11:
            pending.append(available[self.member_pick[1]][0])
        elif part == 8 and self.member_pick and self.member_pick[0] == 2:
            del pending[self.member_pick[1]]
        elif part == 12:  # OK
            g.members = list(pending)
            self.pending_members = None
            self.open_screens.discard(301)
            return [
                ("CLOSE", 301, [], []),
                ("UPDATE", 300, [], self._open_menu_screen(300)[0][3]),
            ]
        self.member_pick = None
        parts = self._group_parts(FakeGroup(g.name, members=pending), 301)
        return [("UPDATE", 301, [], parts)]

    def _group_settings_event(self, ev: dict[str, Any]) -> list[Update]:
        part, event, number = ev["part"], ev["event"], ev["number"]
        g = self.groups[self.selected_group or 0]
        if part == 5 and event == 1:
            self.pending_settings["differential"] = number
            return [("UPDATE", 302, [], [])]
        if part in {8, 9, 10}:
            self.pending_settings["reference"] = part - 8
            return [
                (
                    "UPDATE",
                    302,
                    [],
                    [(p, "CHECK_STATE", 1 if p == part else 0) for p in (8, 9, 10)],
                )
            ]
        if part == 11 and not self.confirming_settings:  # OK asks first
            self.confirming_settings = True
            return [
                (
                    "DLG_OPEN",
                    302,
                    [30, 2, "Execute setting?", "Confirm", "Yes", "No"],
                    [],
                )
            ]
        if part == 30 and self.confirming_settings:
            self.confirming_settings = False
            if event == 1:
                g.differential = self.pending_settings.get(
                    "differential", g.differential
                )
                g.reference = self.pending_settings.get("reference", g.reference)
                self.pending_settings = {}
                self.open_screens.discard(302)
                return [("DLG_CLOSE", 302, [30, 2], []), ("CLOSE", 302, [], [])]
            return [("DLG_CLOSE", 302, [30, 2], [])]
        return []

    def _open_menu_screen(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, target: int
    ) -> list[Update]:
        if target == 100:
            return [("OPEN", 100, [-1], [(4, "SCHEDULE_PANEL", [self.schedules, 0])])]
        if target == 200:
            rows = [
                {"text": ["Enable" if on else "", n], "a": [-1, -1], "b": [-1, -1]}
                for n, on in self.emgstop
            ]
            return [
                ("OPEN", 200, [-1], [(2, "LIST_ROW", {"rows": rows, "selected": []})])
            ]
        if target == 300:
            rows = [
                {
                    "text": [
                        "Enable" if g.enabled else "",
                        g.name,
                        str(len(g.members)),
                    ],
                    "a": [-1] * 3,
                    "b": [-1] * 3,
                }
                for g in self.groups
            ]
            return [
                ("OPEN", 300, [-1], [(2, "LIST_ROW", {"rows": rows, "selected": []})])
            ]
        if target == 700:
            opts = ["30 min", "60 min", "90 min", "120 min", "150 min", "180 min"]
            return [
                (
                    "OPEN",
                    700,
                    [-1],
                    [(2, "COMBO_STRS", opts), (2, "SINGLE_SELIDX", self.overtime_idx)],
                )
            ]
        if target == 3000:
            parts: list[tuple[int, str, Any]] = []
            for p, key in (
                (4, "high_up"),
                (8, "high_back"),
                (13, "low_up"),
                (17, "low_back"),
            ):
                parts += [
                    (p, "COMBO_NUMS", [1, 2, 3, 4, 5, 6, 7]),
                    (p, "SINGLE_SELIDX", self.setback[key] - 1),
                ]
            return [("OPEN", 3000, [-1], parts)]
        if target == 2400:
            parts = [
                (2, "TEXT", self.clock_text),
                (6, "CHECK_STATE", 1 if self.dst else 0),
                (7, "CHECK_STATE", 0 if self.dst else 1),
                *self._dst_parts(),
            ]
            return [("OPEN", 2400, [-1], parts)]
        if target == 2500:
            parts = [
                (2, "TEXT", self.language),
                (5, "COMBO_STRS", ["DD/MM/YYYY", "MM/DD/YYYY", "YYYY/MM/DD"]),
                (5, "SINGLE_SELIDX", self.date_format),
                (7, "CHECK_STATE", 1 if self.time_24h else 0),
                (8, "CHECK_STATE", 0 if self.time_24h else 1),
                (10, "CHECK_STATE", 0 if self.fahrenheit else 1),
                (11, "CHECK_STATE", 1 if self.fahrenheit else 0),
                (13, "CHECK_STATE", 0 if self.decimal_comma else 1),
                (14, "CHECK_STATE", 1 if self.decimal_comma else 0),
            ]
            return [("OPEN", 2500, [-1], parts)]
        if target == 2600:
            return [("OPEN", 2600, [-1], [(1, "TEXT", self.version_text)])]
        if target == 800:
            self.history_loading = True
            self.history_loaded_day = self.history_day
            self.history_job += 1
            columns = [[c, 60, 0, 0, 0] for c in HISTORY_COLUMNS]
            parts = [
                (1, "LIST_COLUMN", columns),
                (8, "TEXT", self.history_day),
                *[
                    (p, "CHECK_STATE", 1)
                    for p in (3, 4, 5, 6)
                    if p in self.history_classes
                ],
                (15, "ENABLED", 0),
                (16, "ENABLED", 0),
            ]
            self.history_page = 0
            progress = [
                19,
                11,
                "Now Loading.",
                self.history_job,
                1,
                "Progress",
                "Cancel",
            ]
            return [("OPEN", 800, [-1], parts), ("DLG_OPEN", 800, progress, [])]
        return []

    def _settings_write(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update] | None:
        """Combo picks and OK on the setback and overtime screens."""
        screen, part, number = ev["screen"], ev["part"], ev["number"]
        keys = {4: "high_up", 8: "high_back", 13: "low_up", 17: "low_back"}
        if screen == 3000 and part in keys:
            self.pending_setback[keys[part]] = number + 1
            return [("UPDATE", 3000, [], [])]
        if screen == 3001 and part in {4, 8}:
            self.pending_recovery["cool" if part == 4 else "heat"] = number + 1
            return [("UPDATE", 3001, [], [])]
        if screen == 700 and part == 2:
            self.pending_overtime = number
            return [("UPDATE", 700, [], [])]
        if screen == 3000 and part == 20:
            self.setback.update(self.pending_setback)
            self.pending_setback = {}
            self.open_screens.discard(3000)
            return [("CLOSE", 3000, [], [])]
        if screen == 3001 and part == 10:
            self.recovery.update(self.pending_recovery)
            self.pending_recovery = {}
            self.open_screens.discard(3001)
            return [("CLOSE", 3001, [], [])]
        if screen == 700 and part == 3:
            if self.pending_overtime is not None:
                self.overtime_idx = self.pending_overtime
            self.pending_overtime = None
            self.open_screens.discard(700)
            return [("CLOSE", 700, [], [])]
        if (screen, part) in {(3000, 21), (3001, 11), (700, 4)}:
            self.pending_setback, self.pending_recovery, self.pending_overtime = (
                {},
                {},
                None,
            )
        return None

    def _optional_parts(self, target: int) -> list[tuple[int, str, Any]]:
        list_part = {400: 3, 500: 3, 600: 2, 900: 1}[target]
        rows = (
            [{"text": ["", "1:1-00"], "a": [-1, -1], "b": [-1, -1]}]
            if target == 600
            else []
        )
        return [(list_part, "LIST_ROW", {"rows": rows, "selected": []})]

    def _group_parts(self, g: FakeGroup, target: int) -> list[tuple[int, str, Any]]:
        if target == 301:
            members = [{"text": [m], "a": [-1], "b": [-1]} for m in g.members]
            available = [
                {"text": [name, other, "Able"], "a": [-1] * 3, "b": [-1] * 3}
                for name, other in self._available(g.members)
            ]
            return [
                (2, "LIST_ROW", {"rows": members, "selected": []}),
                (11, "LIST_ROW", {"rows": available, "selected": []}),
            ]
        return [
            (2, "TEXT", g.name),
            (5, "COMBO_NUMS", [0, 1, 2, 3, 4, 5, 6, 7]),
            (5, "SINGLE_SELIDX", g.differential),
            *[
                (p, "CHECK_STATE", 1 if i == g.reference else 0)
                for i, p in enumerate((8, 9, 10))
            ],
        ]

    def _history_event(self, ev: dict[str, Any]) -> list[Update]:  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        part, event, number, array = ev["part"], ev["event"], ev["number"], ev["array"]
        if event == 5:  # UPD_PROG: the list is loaded on the second request
            if self.history_loading:
                self.history_loading = False
                return [("DLG_UPDATE", 800, [19, 11, 50], [])]
            return [("DLG_CLOSE", 800, [19, 11], [])]
        if part in {3, 4, 5, 6} and event == 1:
            (self.history_classes.add if number else self.history_classes.discard)(part)
            return [("UPDATE", 800, [], [(part, "CHECK_STATE", number)])]
        if part == 11:
            shown = datetime.strptime(self.history_day, "%d/%m/%Y")  # noqa: DTZ007
            current = (shown.year, shown.month - 1, shown.day)
            data = [HISTORY_DATE_ID, 9, 6, "day", *current, 2000, 0, 1, 2037, 11, 31]
            return [("DLG_OPEN", 800, [*data, "OK", "Cancel"], [])]
        if part == HISTORY_DATE_ID:
            if event == 1:
                year, month, day = array[:3]
                self.history_day = f"{day:02d}/{month + 1:02d}/{year}"
            updates: list[Any] = [("DLG_CLOSE", 800, [part, 9], [])]
            return [*updates, ("UPDATE", 800, [], [(8, "TEXT", self.history_day)])]
        if part in {12, 15, 16}:
            if part == 12:
                self.history_loaded_day = self.history_day
                self.history_page = 0
            elif self._history_page_enabled(part):
                self.history_page += 1 if part == 15 else -1
            else:
                self.history_page = -1
            self.history_loading = True
            self.history_job += 1
            progress = [
                19,
                11,
                "Now Loading.",
                self.history_job,
                1,
                "Progress",
                "Cancel",
            ]
            return [("DLG_OPEN", 800, progress, [])]
        return []

    def _history_matching(self) -> list[list[str]]:
        """Return the rows the class filters and the loaded day show."""
        kinds = {3: "Error", 4: "Control", 5: "Attributes", 6: "Status"}
        wanted = {kinds[c] for c in self.history_classes}
        return [
            r
            for r in self.history_rows
            if r[1] in wanted and self.history_loaded_day == "01/01/2025"
        ]

    def _history_page_enabled(self, part: int) -> bool:
        """Whether a page button (15 older, 16 newer) is enabled."""
        if self.history_page < 0:
            return False
        if part == 15:
            older = (self.history_page + 1) * self.history_page_size
            return older < len(self._history_matching())
        return self.history_page > 0

    def _polling(self, _ev: dict[str, Any]) -> list[Update]:
        """Deliver the history once it has loaded."""
        if 800 not in self.open_screens or self.history_loading:
            return []
        matching = self._history_matching()
        size, page = self.history_page_size, self.history_page
        rows = matching[page * size : (page + 1) * size] if page >= 0 else []
        first = page * size + 1
        label = f"{first}-{first + len(rows) - 1}/{len(matching)}" if rows else "0-0/0"
        parts: list[tuple[int, str, Any]] = [
            (
                1,
                "LIST_ROW",
                {
                    "rows": [{"text": r, "a": [-1] * 10, "b": [-1] * 10} for r in rows],
                    "selected": [],
                },
            ),
            (14, "LABEL", label),
            (15, "ENABLED", int(self._history_page_enabled(15))),
            (16, "ENABLED", int(self._history_page_enabled(16))),
        ]
        return [("UPDATE", 800, [], parts)]

    def _error_information(self, _ev: dict[str, Any]) -> list[Update]:
        if not self.errors:
            return []
        self.error_shown = True
        parts: list[tuple[int, str, Any]] = [
            (0, "LABEL", "Error Information"),
            (3, "LIST_COLUMN", [["Name", 150, 0, 0, 0], ["Code", 60, 0, 0, 0]]),
            (
                3,
                "LIST_ROW",
                {
                    "rows": [
                        {"text": r, "a": [-1] * len(r), "b": [-1] * len(r)}
                        for r in self.errors
                    ],
                    "selected": [],
                },
            ),
            (4, "LABEL", "Contact"),
            (5, "TEXT", "0800-1234"),
        ]
        return [("OPEN", 10, [-1], parts)]

    def _close_errors(self, _ev: dict[str, Any]) -> list[Update]:
        self.error_shown = False
        return [("CLOSE", 10, [], [])]
