"""The simulated iTM's schedule screens."""

from __future__ import annotations

from typing import Any

from .fake_itm_base import (
    EVENT_MODES,
    FakeITMBase,
    Update,
    _enabled,
)


class ScheduleMixin(FakeITMBase):
    """The schedule screens and the event editor."""

    # -- schedules ------------------------------------------------------------------
    def _program(self) -> list[Any]:
        return self.schedules[self.selected_program]

    def _overview_day(self) -> tuple[str, int]:
        """Return the shown day's label and weekday (01/01/2025 plus the offset)."""
        base = 1 + self.overview_offset
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        weekday = (3 + self.overview_offset) % 7
        return f"{base:02d}/01({names[weekday]})", weekday

    def _current_events(self) -> list[dict[str, Any]]:
        name = self._program()[1]
        if self.editor_list == "weekly":
            return self.schedule_events.get(name, {}).get(self.editor_day, [])
        label = f"Ex{self.editor_special + 1}"
        return self.special_events.get(name, {}).get(label, [])

    @staticmethod
    def _event_rows(events: list[dict[str, Any]]) -> dict[str, Any]:
        rows = []
        for e in events:
            icon = 1000 if e["kind"] == "zone" else 2114
            power = e.get("power")
            action = "" if power is None else "Start" if power == 0 else "Stop"
            rows.append(
                {
                    "text": [e["time"], e["target"], action],
                    "a": [-1, icon, -1],
                    "b": [-1, 0 if e.get("power") == 0 else 2, -1],
                }
            )
            rows.append({"text": ["", "", ""], "a": [4016, -1, -1], "b": [-1, -1, -1]})
        return {"rows": rows, "selected": []}

    def _bar(self, events: list[dict[str, Any]]) -> list[list[int]]:
        minutes = {int(e["time"][:2]) * 60 + int(e["time"][3:]) for e in events}
        return [[slot, 0 if slot * 15 in minutes else -1, -1, 2] for slot in range(96)]

    def _editor_parts(self) -> list[tuple[int, str, Any]]:
        state, name, start, _end = self._program()
        parts: list[tuple[int, str, Any]] = [
            (3, "TEXT", name),
            (7, "CHECK_STATE", 0 if state in (3, 4) else 1),
            (8, "CHECK_STATE", 1 if state in (3, 4) else 0),
            (10, "CHECK_STATE", 1 if start else 0),
            (
                16,
                "COMBO_STRS",
                [
                    "Jan",
                    "Feb",
                    "Mar",
                    "Apr",
                    "May",
                    "Jun",
                    "Jul",
                    "Aug",
                    "Sep",
                    "Oct",
                    "Nov",
                    "Dec",
                ],
            ),
            (16, "SINGLE_SELIDX", 4),
            (17, "COMBO_NUMS", list(range(1, 32))),
            (17, "SINGLE_SELIDX", 0),
            (
                18,
                "COMBO_STRS",
                [
                    "Jan",
                    "Feb",
                    "Mar",
                    "Apr",
                    "May",
                    "Jun",
                    "Jul",
                    "Aug",
                    "Sep",
                    "Oct",
                    "Nov",
                    "Dec",
                ],
            ),
            (18, "SINGLE_SELIDX", 8),
            (19, "COMBO_NUMS", list(range(1, 32))),
            (19, "SINGLE_SELIDX", 29),
            (
                26,
                "LIST_ROW",
                {
                    "rows": [
                        {"text": r, "a": [-1] * 3, "b": [-1] * 3}
                        for r in self.calendar_rows
                    ],
                    "selected": [],
                },
            ),
        ]
        weekly = self.schedule_events.get(name, {})
        parts += [
            (64 + d, "TIMELINE_BAR", self._bar(weekly.get(d, []))) for d in range(7)
        ]
        special = self.special_events.get(name, {})
        parts += [
            (95 + i, "TIMELINE_BAR", self._bar(special.get(f"Ex{i + 1}", [])))
            for i in range(5)
        ]
        return parts

    def _event_parts(self, e: dict[str, Any]) -> list[tuple[int, str, Any]]:
        parts: list[tuple[int, str, Any]] = [
            (43, "TEXT", e["time"]),
            (46, "TEXT", e["target"]),
            (2, "CHECK_STATE", 1),
            *[
                (p, "CHECK_STATE", 1 if i == e.get("power") else 0)
                for i, p in enumerate((3, 4, 54, 55))
            ],
        ]
        if "locks" in e:
            for check, radios, value in (
                (7, (8, 9, 10), e["locks"][0]),
                (11, (12, 13), e["locks"][1]),
                (14, (15, 16), e["locks"][2]),
            ):
                parts.append((check, "CHECK_STATE", 1))
                parts += [
                    (p, "CHECK_STATE", 1 if i == value else 0)
                    for i, p in enumerate(radios)
                ]
        if "mode_idx" in e:
            parts += [
                (17, "CHECK_STATE", 1),
                (18, "COMBO_STRS", EVENT_MODES),
                (18, "SINGLE_SELIDX", e["mode_idx"]),
            ]
        if "setpoint" in e:
            parts += [
                (19, "CHECK_STATE", 1),
                (20, "CHECK_STATE", 1),
                (21, "TEXT", f"{e['setpoint']:.1f}"),
                (24, "CHECK_STATE", 0),
            ]
        if "vent" in e:
            parts += [
                (33, "CHECK_STATE", 1),
                (34, "COMBO_STRS", ["Automatic", "Bypass"]),
                (34, "SINGLE_SELIDX", e["vent"]),
            ]
        return parts

    def _schedule_create_delete(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update] | None:
        """Add (a new program in the editor), its rename, and Delete."""
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if screen == 100 and part == 1 and event == 1:
            self.open_screens.add(104)
            self.new_program = "New Schedule 001"
            self.editor_day, self.editor_special, self.editor_list = 0, 0, "weekly"
            parts = [
                (0, "LABEL", self.new_program),
                (3, "TEXT", self.new_program),
                (8, "CHECK_STATE", 1),
            ]
            return [("OPEN", 104, [-1], parts)]
        if screen == 104 and part == 4 and self.new_program is not None:
            self.renaming = True
            data = [11, 4, "Name", self.new_program, 1, 32, 0, 0, 1, "", "OK"]
            return [("DLG_OPEN", 104, [*data, "Cancel", *[""] * 12, ""], [])]
        if screen == 104 and part == 11 and self.renaming:
            self.renaming = False
            if event == 1:
                self.new_program = ev["string"]
                return [
                    ("UPDATE", 104, [], [(3, "TEXT", self.new_program)]),
                    ("DLG_CLOSE", 104, [11, 4], []),
                ]
            return [("DLG_CLOSE", 104, [11, 4], [])]
        if screen == 100 and part == 10 and event == 1:
            self.deleting_program = True
            name = self._program()[1]
            confirm = [14, 2, f"{name} delete?", "Confirm", "Yes", "No"]
            return [("DLG_OPEN", 100, confirm, [])]
        if screen == 100 and part == 14 and self.deleting_program:
            self.deleting_program = False
            if event == 1:
                del self.schedules[self.selected_program]
                self.selected_program = 0
                panel = [(4, "SCHEDULE_PANEL", [self.schedules, 0])]
                return [("DLG_CLOSE", 100, [14, 2], []), ("UPDATE", 100, [], panel)]
            return [("DLG_CLOSE", 100, [14, 2], [])]
        return None

    def _schedule_event(  # noqa: PLR0911, PLR0912, PLR0915  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements,too-many-locals
        self, ev: dict[str, Any]
    ) -> list[Update]:
        screen, part, event, number = (
            ev["screen"],
            ev["part"],
            ev["event"],
            ev["number"],
        )
        created = self._schedule_create_delete(ev)
        if created is not None:
            return created
        if screen == 100 and part == 4 and event == 1:
            self.selected_program = number
            return [("UPDATE", 100, [], [(5, "LABEL", self._program()[1])])]
        if screen == 100 and part == 8:
            self.open_screens.add(102)
            label, weekday = self._overview_day()
            events = self.schedule_events.get(self._program()[1], {}).get(weekday, [])
            parts = [
                (0, "LABEL", f"Schedule:{self._program()[1]}"),
                (2, "LABEL", label),
                (4, "LIST_ROW", self._event_rows(events)),
            ]
            return [("OPEN", 102, [-1], parts)]
        if screen == 102 and part in {1, 3}:
            self.overview_offset += 1 if part == 3 else -1
            label, weekday = self._overview_day()
            events = self.schedule_events.get(self._program()[1], {}).get(weekday, [])
            return [
                (
                    "UPDATE",
                    102,
                    [],
                    [(2, "LABEL", label), (4, "LIST_ROW", self._event_rows(events))],
                )
            ]
        if screen == 100 and part == 11:
            self.open_screens.add(104)
            self.editor_day, self.editor_special, self.editor_list = 0, 0, "weekly"
            return [("OPEN", 104, [-1], self._editor_parts())]
        if screen == 104 and part == 1 and event == 8:
            return [("UPDATE", 104, [], [])]
        if screen == 104 and 52 <= part <= 58:
            self.editor_day, self.editor_list = part - 52, "weekly"
            return [("UPDATE", 104, [], [])]
        if screen == 104 and 85 <= part <= 89:
            self.editor_special, self.editor_list = part - 85, "special"
            return [("UPDATE", 104, [], [])]
        if screen == 104 and part in {73, 102}:
            self.open_screens.add(107)
            self.selected_event = None
            return [
                (
                    "OPEN",
                    107,
                    [-1],
                    [
                        (0, "LABEL", self._program()[1]),
                        (4, "LIST_ROW", self._event_rows(self._current_events())),
                    ],
                )
            ]
        if screen == 107 and part == 4 and event == 1:
            # Rows alternate event, separator: only an event row enables the buttons.
            row = ev["array"][0] if ev["array"] else number
            self.selected_event = row // 2 if row % 2 == 0 else None
            enabled = 1 if self.selected_event is not None else 0
            return [("UPDATE", 107, [], _enabled((6, 7, 8), enabled))]
        if screen == 107 and part == 8:
            if self.selected_event is None:
                msg = "Edit clicked while disabled: the real iTM hangs here"
                raise RuntimeError(msg)
            self.open_screens.add(109)
            e = self._current_events()[self.selected_event]
            return [("OPEN", 109, [-1], self._event_parts(e))]
        if screen == 104 and part in {7, 8}:
            self.pending_program_enabled = part == 7
            return [
                (
                    "UPDATE",
                    104,
                    [],
                    [
                        (7, "CHECK_STATE", int(part == 7)),
                        (8, "CHECK_STATE", int(part == 8)),
                    ],
                )
            ]
        if screen == 104 and part == 20 and event == 1:
            self.editor_saving = True
            return [
                (
                    "DLG_OPEN",
                    104,
                    [25, 2, "Execute setting?", "Confirm", "Yes", "No"],
                    [],
                )
            ]
        if screen == 104 and part == 25 and self.editor_saving:
            self.editor_saving = False
            if event == 1 and self.new_program is not None:
                self.schedules.append([3, self.new_program, "", ""])
                self.new_program = None
                self.open_screens.discard(104)
                return [("DLG_CLOSE", 104, [25, 2], []), ("CLOSE", 104, [], [])]
            if event == 1 and self.pending_program_enabled is not None:
                program = self._program()
                program[0] = 0 if self.pending_program_enabled else 3
                self.pending_program_enabled = None
                self.open_screens.discard(104)
                return [("DLG_CLOSE", 104, [25, 2], []), ("CLOSE", 104, [], [])]
            return [("DLG_CLOSE", 104, [25, 2], [])]
        if screen == 104 and part == 21 and event == 1:
            self.editor_confirming = True
            return [
                (
                    "DLG_OPEN",
                    104,
                    [24, 2, "Confirm exit?", "Confirm", "Yes", "No"],
                    [],
                )
            ]
        if screen == 104 and part == 24 and self.editor_confirming:
            self.editor_confirming = False
            if event == 1:  # Yes: leave without saving
                self.open_screens.discard(104)
                self.new_program = None
                return [("DLG_CLOSE", 104, [24, 2], []), ("CLOSE", 104, [], [])]
            return [("DLG_CLOSE", 104, [24, 2], [])]
        return []
