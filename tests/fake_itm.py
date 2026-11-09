"""A small simulated iTM that behaves like the real one where it matters.

- one web session at a time; logins are rejected while another session is active
- replies only contain operation-panel parts that differ from what the client is
  believed to show, and a client's own selection is never echoed back
- setpoint changes are applied on the TIMER commit, and ignored while a unit is
  stopped
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from daikin_itm.protocol import (
    _DIALOG_LAYOUTS,
    PART_UPDATE_CODE,
    SCREEN_UPDATE_CODE,
    Reader,
    Writer,
)

from .fake_itm_base import (
    CAPACITY_OPTIONS,
    DST_HOURS,
    EVENT_MODES,
    EXTRA_COMBOS,
    EXTRA_CONTROLS,
    EXTRA_NUMBERS,
    EXTRA_OWNER,
    EXTRA_RADIOS,
    HARDWARE_OPTIONS,
    HISTORY_COLUMNS,
    HISTORY_ROWS,
    LIST_HEADERS,
    LOCK_LABELS,
    LOW_NOISE_OPTIONS,
    MENU_CLOSE_PARTS,
    MENU_SCREENS,
    MONTHS,
    MULTISTATE_OPTIONS,
    OPTIONAL_BUTTONS,
    POWER_OPTIONS,
    ROW_LISTS,
    SCREENSAVER_MODES,
    SERVICE_BUTTONS,
    SETTINGS_TABS,
    SYSTEM_BUTTONS,
    SYSTEM_WRITE_SCREENS,
    WEEKDAYS,
    WEEKS,
    FakeAnalog,
    FakeGroup,
    FakeUnit,
    FakeVentilation,
    FakeZone,
    Update,
    _enabled,
    _rows,
)
from .fake_itm_menu import MenuMixin
from .fake_itm_panel import PanelMixin
from .fake_itm_system import SystemWrites

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "CAPACITY_OPTIONS",
    "DST_HOURS",
    "EVENT_MODES",
    "EXTRA_COMBOS",
    "EXTRA_CONTROLS",
    "EXTRA_NUMBERS",
    "EXTRA_OWNER",
    "EXTRA_RADIOS",
    "HARDWARE_OPTIONS",
    "HISTORY_COLUMNS",
    "HISTORY_ROWS",
    "LIST_HEADERS",
    "LOCK_LABELS",
    "LOW_NOISE_OPTIONS",
    "MENU_CLOSE_PARTS",
    "MENU_SCREENS",
    "MONTHS",
    "MULTISTATE_OPTIONS",
    "OPTIONAL_BUTTONS",
    "POWER_OPTIONS",
    "ROW_LISTS",
    "SCREENSAVER_MODES",
    "SERVICE_BUTTONS",
    "SETTINGS_TABS",
    "SYSTEM_BUTTONS",
    "SYSTEM_WRITE_SCREENS",
    "WEEKDAYS",
    "WEEKS",
    "_ENCODERS",
    "FakeAnalog",
    "FakeGroup",
    "FakeITM",
    "FakeUnit",
    "FakeVentilation",
    "FakeZone",
    "SystemWrites",
    "Update",
    "_enabled",
    "_encode_data",
    "_encode_part",
    "_rows",
]


class FakeITM(PanelMixin, MenuMixin):
    """The simulated iTM; feed it request bodies with handle()."""

    # -- request handling ---------------------------------------------------------
    def handle(self, body: str) -> str:
        """Answer a request body (``snD=...``) like the iTM would."""
        if (error := self.fail_next) is not None:
            self.fail_next = None
            raise error  # pylint: disable=raising-bad-type  # inferred from __init__
        r = Reader(body[4:])
        token = r.raw(32)
        ev: dict[str, Any] = {
            "screen": r.short(),
            "part": r.short(),
            "event": r.short(),
            "dtype": r.short(),
            "number": r.int(),
        }
        n = r.short()
        ev["array"] = [r.int() for _ in range(n)]
        ev["string"] = r.string()
        ev["token"] = token
        self.requests.append(ev)

        if ev["event"] == 0:  # INIT
            if ev["string"] != self.web_version:
                return self._reply(None, [("LOGOUT", -1, [2, self.web_version], [])])
            return self._reply(None, [("OPEN", 1203, [-1], [(0, "LABEL", "User")])])
        if ev["screen"] == 1203 and ev["event"] == 1:
            return self._login(ev["string"])
        if self.session is None or token != self.session:
            return self._reply(None, [("LOGOUT", -1, [3], [])])
        if ev["event"] == 10:  # logout
            self.session = None
            return self._reply(None, [])
        return self._reply(self.session, self._list_or_event(ev))

    def _list_or_event(self, ev: dict[str, Any]) -> list[Update]:
        if ev["event"] == 1 and (ev["screen"], ev["part"]) in ROW_LISTS:
            # A list takes its selection as INTARRAY (the web UI's
            # onSelectedMultiItem); the real iTM ignores a plain INT there.
            if ev["dtype"] != 8 or not ev["array"]:
                return []
            ev = {**ev, "dtype": 1, "number": ev["array"][0]}
        return self._event(ev)

    def _login(self, s: str) -> str:
        n = int(s[:4])
        user, digest = s[4 : 4 + n], s[4 + n :]
        if self.session is not None or user != self.username or digest != self.digest:
            return self._reply(
                None,
                [("DLG_OPEN", 1203, [6, 1, "Cannot log in", "Error", "OK"], [])],
            )
        self.session = next(self.tokens)
        for p in self._pending:
            p[0] -= 1
            if p[0] <= 0:
                p[1]()
        self._pending = [p for p in self._pending if p[0] > 0]
        self.path = [self.root]
        self.selected = 0
        self.shown = {}
        self.pending_setpoint = None
        self.pending_power = None
        self.confirming_setback = None
        self.service_mode = False
        self.corners = []
        return self._reply(
            self.session,
            [
                ("LOGIN", -1, [102, "Information", ""], []),
                ("OPEN", 0, [-1], self._menu_bar()),
                ("OPEN", 1, [-1], [(6, "ICONPANEL", self._icons())]),
                ("OPEN", 4, [-1], self._panel_delta()),
            ],
        )

    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        routes: list[tuple[bool, Callable[[dict[str, Any]], list[Update]]]] = [
            (screen == 1 and part == 6 and event == 1, self._select_icon),
            (screen == 1 and part == 2 and event == 1, self._zone_down),
            (screen == 1 and part == 4 and event == 1, self._list_view),
            (screen == 2 and part == 6 and event == 1, self._icon_view),
            (screen == 0 and part == 19 and event == 1, self._error_information),
            (screen == 10 and part == 1 and event == 1, self._close_errors),
            (event == 4, self._polling),
            (screen in {0, 5} or screen in self.open_screens, self._menu),
            (screen == 4 and event == 1 and part in {9, 11}, self._choose),
            (
                screen == 4 and part == 24 and self.confirming_setback is not None,
                self._confirm_setback,
            ),
            (screen == 4 and part == 17 and event == 1, self._open_dialog),
            (screen == 4 and part == 7 and event == 1, self._details),
            (screen == 9 and part == 15, lambda _: [("CLOSE", 9, [], [])]),
            (screen == 7, self._dialog_event),
            (screen == 4 and event == 3, self._commit),
        ]
        for matches, handler in routes:
            if matches:
                return handler(ev)
        return []

    # -- encoding -------------------------------------------------------------------
    @staticmethod
    def _reply(token: str | None, updates: list[Update]) -> str:
        w = Writer(token or "0" * 32)
        w.int(len(updates))
        for kind, screen, data, parts in updates:
            w.short(SCREEN_UPDATE_CODE[kind])
            w.short(screen)
            _encode_data(w, kind, data)
            if kind in {"OPEN", "UPDATE", "CLOSE", "SHOW", "HIDE"}:
                w.int(len(parts))
                for part, ptype, value in parts:
                    w.short(part)
                    w.short(PART_UPDATE_CODE[ptype])
                    _encode_part(w, ptype, value)
        return w.buf + "\0"


def _encode_data(  # noqa: PLR0912, PLR0915  # pylint: disable=too-many-branches,too-many-statements
    w: Writer, kind: str, data: list[Any]
) -> None:
    if kind == "OPEN":
        w.int(data[0])
    elif kind == "LOGIN":
        w.int(data[0])
        w.string(data[1])
        w.string(data[2])
    elif kind == "LOGOUT":
        w.int(data[0])
        if data[0] == 2:
            w.string(data[1])
    elif kind == "DLG_CLOSE":
        w.short(data[0])
        w.short(data[1])
    elif kind == "DLG_OPEN" and data[1] == 9:  # DATE: N S + 9 H + h + 13 S
        w.short(data[0])
        w.short(data[1])
        w.int(data[2])
        w.string(data[3])
        for n in data[4:13]:
            w.short(n)
        w.short(0)
        for text in [*data[13:], *[""] * 13][:13]:
            w.string(text)
    elif kind == "DLG_OPEN" and data[1] == 4:  # CHARINPUT, see _DIALOG_LAYOUTS
        w.short(data[0])
        w.short(data[1])
        w.string(data[2])
        w.string(data[3])
        for n in data[4:9]:
            w.short(n)
        w.short(0)
        for text in data[9:23]:
            w.string(text)
        w.string("")
        w.string("")
        w.string("")
        w.string(data[23])
    elif kind == "DLG_OPEN" and data[1] in {5, 8}:  # PASSWORD, TIME: by layout
        w.short(data[0])
        w.short(data[1])
        values = iter(data[2:])
        for code in _DIALOG_LAYOUTS[data[1]]:
            value = next(values) if code.isupper() else None
            if code.upper() == "S":
                w.string(value or "")
            elif code.upper() == "H":
                w.short(value or 0)
            else:
                w.int(value or 0)
    elif kind == "DLG_OPEN" and data[1] == 6:  # NUMBER: NSNNNNNNHH + 11 S
        w.short(data[0])
        w.short(data[1])
        w.int(data[2])
        w.string(data[3])
        for n in data[4:10]:
            w.int(n)
        w.short(data[10])
        w.short(data[11])
        for text in data[12:23]:
            w.string(text)
    elif kind == "DLG_OPEN":
        w.short(data[0])
        w.short(data[1])
        for v in data[2:]:
            if isinstance(v, int):
                w.int(v)
            else:
                w.string(v)
    elif kind == "DLG_UPDATE":
        w.short(data[0])
        w.short(data[1])
        if data[1] == 11:
            w.int(data[2])


def _encode_part(w: Writer, ptype: str, v: Any) -> None:
    if ptype in {"LABEL", "TEXT"}:
        w.string(v)
    elif ptype in {"VISIBLED", "ENABLED", "SINGLE_SELIDX", "ICONID", "CHECK_STATE"}:
        w.int(v)
    elif ptype == "ICONID_MODE":
        w.short(v[0])
        w.short(v[1])
    elif ptype == "COMBO_STRS":
        w.int(len(v))
        for s in v:
            w.string(s)
    elif ptype == "COMBO_NUMS":
        w.int(len(v))
        for n in v:
            w.int(n)
    elif ptype in _ENCODERS:
        _ENCODERS[ptype](w, v)
    else:
        raise NotImplementedError(ptype)


def _encode_iconpanel(w: Writer, v: dict[str, Any]) -> None:
    w.int(len(v["icons"]))
    for name, a, b in v["icons"]:
        w.string(name)
        w.short(a)
        w.short(b)
    w.int(v["selected"])


def _encode_list_column(w: Writer, v: list[list[Any]]) -> None:
    w.int(len(v))
    for name, *shorts in v:
        w.string(name)
        for n in shorts:
            w.short(n)


def _encode_list_row(w: Writer, v: dict[str, Any]) -> None:
    rows = v["rows"]
    ncols = len(rows[0]["text"]) if rows else 0
    w.short(len(rows))
    w.short(ncols)
    for row in rows:
        for text in row["text"]:
            w.string(text)
        for n in row["a"]:
            w.short(n)
        for n in row["b"]:
            w.short(n)
    w.int(len(v["selected"]))
    for n in v["selected"]:
        w.int(n)


def _encode_pntlist(w: Writer, v: dict[str, Any]) -> None:
    w.int(len(v["cols"]))
    for name, width in v["cols"]:
        w.string(name)
        w.int(width)
    w.short(v["sort"][0])
    w.short(v["sort"][1])
    w.int(len(v["rows"]))
    for row in v["rows"]:
        for text, a, b in row:
            w.string(text)
            w.short(a)
            w.short(b)
    w.int(v["selected"])


def _encode_tab_name(w: Writer, v: list[str]) -> None:
    w.int(len(v))
    for name in v:
        w.string(name)


def _encode_tab_visible(w: Writer, v: list[Any]) -> None:
    w.short(v[0])
    w.short(len(v[1]))
    for n in v[1]:
        w.int(n)


def _encode_schedule_panel(w: Writer, v: list[Any]) -> None:
    w.int(len(v[0]))
    for state, name, start, end in v[0]:
        w.int(state)
        w.string(name)
        w.string(start)
        w.string(end)
    w.int(v[1])


def _encode_timeline_bar(w: Writer, v: list[list[int]]) -> None:
    w.int(len(v))
    for entry in v:
        for n in entry:
            w.short(n)


def _encode_list_cell(w: Writer, v: list[list[Any]]) -> None:
    w.int(len(v))
    for row, col, text, a, b in v:
        w.short(row)
        w.short(col)
        w.string(text)
        w.short(a)
        w.short(b)


_ENCODERS: dict[str, Callable[[Writer, Any], None]] = {
    "LIST_CELL": _encode_list_cell,
    "TIMELINE_BAR": _encode_timeline_bar,
    "TAB_NAME": _encode_tab_name,
    "TAB_VISIBLE": _encode_tab_visible,
    "SCHEDULE_PANEL": _encode_schedule_panel,
    "ICONPANEL": _encode_iconpanel,
    "PNTLIST": _encode_pntlist,
    "LIST_COLUMN": _encode_list_column,
    "LIST_ROW": _encode_list_row,
}
