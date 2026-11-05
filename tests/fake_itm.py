"""A small simulated iTM that behaves like the real one where it matters.

- one web session at a time; logins are rejected while another session is active
- replies only contain operation-panel parts that differ from what the client is
  believed to show, and a client's own selection is never echoed back
- setpoint changes are applied on the TIMER commit, and ignored while a unit is
  stopped
"""

from __future__ import annotations

import functools
import hashlib
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from daikin_itm.protocol import (
    _DIALOG_LAYOUTS,
    PART_UPDATE_CODE,
    SCREEN_UPDATE_CODE,
    Reader,
    Writer,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

POWER_OPTIONS = ["Start", "SB Low", "SB High", "Stop"]

# (update type, screen, data, [(part, part type, value)])
Update = tuple[str, int, list[Any], list[tuple[int, str, Any]]]


@dataclass
class FakeUnit:  # pylint: disable=too-many-instance-attributes
    """An indoor unit."""

    name: str
    running: bool = True
    setpoint: int = 21
    setpoint_range: tuple[int, ...] = (19, 20, 21, 22, 23, 24)
    temp: float = 21.0
    mode: str = "Heat"
    modes: tuple[str, ...] = ("Fan", "Cool", "Heat")
    fan_icon: int | None = 3011  # None: no fan speed control
    point_id: str = "100"
    address: str = "1-00"
    description: str = ""
    # Wall remote locks: index of the checked radio (allowed / stop only / forbidden).
    lock_start_stop: int = 0
    lock_mode: int = 0
    lock_setpoint: int = 0
    # Setpoint limits (enabled, min, max) and the overtime timer.
    cool_limit: tuple[bool, float, float] = (False, 16.0, 32.0)
    heat_limit: tuple[bool, float, float] = (False, 16.0, 32.0)
    overtime: bool = False
    setback: int = 0  # 0 none, 1 low, 2 high (the unit is not running then)
    thermo_on: bool = False
    error_code: str = ""
    airflow: int | None = None  # dialog icon (3101..3111); None: not adjustable
    filter_sign: bool = False
    dust_alarm: bool = False
    # The settings dialog's optional settings (None: not offered by the unit). A unit
    # with a HW supply offers the first ones, a unit with outputs the last ones.
    low_noise: int | None = None  # index into LOW_NOISE_OPTIONS
    outdoor_low_noise: int | None = None
    capacity: int | None = None  # index into CAPACITY_OPTIONS
    reheat: bool | None = None
    leaving_water: tuple[float, float] | None = None  # cool, heat
    storage_water: float | None = None
    output_repeat: bool | None = None
    output_interval: int = 2  # index into 1..10 minutes
    analog_output: float | None = None
    multistate: int | None = None  # index into MULTISTATE_OPTIONS
    r32_alarm_stop: bool = False  # the dialog offers to stop the R32 alarm


LOW_NOISE_OPTIONS = ["Off", "Step 1", "Step 2", "Step 3"]
CAPACITY_OPTIONS = [100, 70, 40, 0]
MULTISTATE_OPTIONS = ["State 1", "State 2", "State 3"]
SETTINGS_TABS = [
    "Common ",
    "R/C Prohibition",
    "A/C",
    "Ventilator",
    "Dio, Ao, Mo",
    "HW Supply",
]
# Optional settings of the settings dialog: checkbox -> the controls ticking enables.
EXTRA_CONTROLS = {
    37: (38, 40),
    94: (96, 97, 98),
    107: (108, 110),
    111: (112, 114),
    115: (116, 118),
    119: (120,),
    123: (124, 126),
    129: (130,),
    140: (141,),
    147: (148,),
}
EXTRA_OWNER = {c: check for check, cs in EXTRA_CONTROLS.items() for c in cs}
EXTRA_RADIOS = {
    96: ("output_repeat", True),
    97: ("output_repeat", False),
    108: ("reheat", True),
    110: ("reheat", False),
}
EXTRA_COMBOS = {
    98: "output_interval",
    120: "low_noise",
    130: "multistate",
    141: "outdoor_low_noise",
    148: "capacity",
}
# Their number inputs: "change" button -> (pending key, kind, low, high).
EXTRA_NUMBERS = {
    40: ("analog_output", 1, 0, 100),
    114: ("lw_cool", 2, 5, 20),
    118: ("lw_heat", 2, 25, 55),
    126: ("storage_water", 2, 30, 70),
}


LIST_HEADERS = [
    "Name",
    "Status",
    "Changeover Option",
    "Mode",
    "Temp.",
    "CO2",
    "Setpoint",
    "Setpoint Restriction(Cool)",
    "Setpoint Restriction(Heat)",
    "Filter Sign",
    "Dust Alarm",
    "Fan Speed",
    "Airflow Direction",
    "On/Off(Reheat)",
    "On/Off(Storage)",
    "Leaving Water Setpoint(Cool)",
    "Leaving Water Setpoint(Heat)",
    "Storage Water Setpoint",
    "Low Noise",
    "R/C Start/Stop",
    "R/C Op. Mode",
    "R/C Setpoint",
    "Inoperable",
    "Error Code",
    "R32 Alarm",
]
LOCK_LABELS = ["", "Stop Only", "Prohibited"]


@dataclass
class FakeVentilation:
    """A heat reclaim ventilation unit: on/off only."""

    name: str
    running: bool = True
    point_id: str = "305"
    address: str = "4-07"
    description: str = "Fresh air"
    # Ventilation mode and air volume options (empty: not offered) and the choice.
    vent_modes: tuple[str, ...] = ()
    vent_mode: int = 0
    vent_amounts: tuple[str, ...] = ()
    vent_amount: int = 0


@dataclass
class FakeAnalog:
    """An analog input watching a unit's temperature."""

    name: str
    alarm: bool = False
    point_id: str = "312"
    source_point: str = "1:1-00"
    upper_limit: float = 30.0


@dataclass
class FakeGroup:
    """An auto changeover group."""

    name: str
    enabled: bool = False
    members: list[str] = field(default_factory=list)
    differential: int = 3
    reference: int = 2  # 0 fixed, 1 operating, 2 average


# Lists of the dealer-option, demand, emergency stop and service screens, by (screen,
# part): their fakes read a selected row as a number, translated from INTARRAY.
ROW_LISTS = {
    (3301, 1),
    (3307, 2),
    (3308, 2),
    (3309, 2),
    (3311, 2),
    (3311, 8),
    (1514, 2),
    (200, 2),
    (201, 1),
    (203, 2),
    (203, 8),
    (400, 3),
    (500, 3),
    (503, 1),
    (600, 2),
    (900, 1),
    (902, 2),
    (902, 10),
    (904, 1),
    (3100, 2),
    (1517, 2),
}

# A history row: time, kind, number, content, name, source, port, address, id, code.
HISTORY_ROWS: list[list[str]] = [
    [
        "18:00",
        "Status",
        "B102",
        "Operation Mode changed(Cool)",
        "1:1-04",
        "Auto Change",
        "1",
        "1-04",
        "105",
        "",
    ],
    [
        "17:55",
        "Status",
        "B103",
        "Setpoint changed(22.5°C)",
        "1:1-04",
        "Schedule",
        "1",
        "1-04",
        "105",
        "",
    ],
    ["17:50", "Control", "A011", "Logged in(x.x.x.x)", "user", "", "", "", "", ""],
]
HISTORY_COLUMNS = [
    "Time",
    "Type",
    "History No.",
    "Contents",
    "Name",
    "Instructed by",
    "Port",
    "Address",
    "ID",
    "Code",
]
MENU_SCREENS = {
    3: 100,
    7: 200,
    9: 300,
    17: 700,
    19: 3000,
    41: 2400,
    43: 2500,
    51: 2600,
    53: 800,
}
EVENT_MODES = ["Fan", "Cool", "Heat", "Setpoint", "Automatic"]
MENU_CLOSE_PARTS = {
    100: 13,
    102: 5,
    107: 10,
    109: 6,
    200: 1,
    300: 1,
    301: 13,
    302: 12,
    700: 4,
    800: 17,
    2400: 19,
    2500: 19,
    2600: 2,
    3000: 21,
    3001: 11,
    400: 1,
    500: 1,
    600: 8,
    900: 11,
}
OPTIONAL_BUTTONS = {11: 400, 13: 500, 15: 600, 5: 900}
SYSTEM_WRITE_SCREENS = {
    2700,
    1700,
    1701,
    1600,
    1603,
    1200,
    1204,
    1202,
    2500,
    2501,
    2400,
    1800,
}
DST_HOURS = {12: ["01:00", "02:00", "03:00", "04:00"], 17: ["02:00", "03:00", "04:00"]}
MONTHS = [
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
]
WEEKS = ["1st", "2nd", "3rd", "4th", "Last"]
WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
SERVICE_BUTTONS = {
    69: 1500,
    77: 1505,
    83: 1509,
    117: 1516,
    73: 1507,
    65: 1501,
    67: 1502,
    111: 1511,
    63: 2800,
    71: 1503,
    75: 1504,
    79: 1508,
    81: 1510,
    85: 3100,
}
SCREENSAVER_MODES = [
    "Disable",
    "Backlight Off",
    "Screen 1",
    "Screen 2",
    "Screen 3",
]
HARDWARE_OPTIONS = {
    3: [str(i) for i in range(1, 9)],
    6: [str(i) for i in range(6)],
    8: ["1 min", "3 min", "5 min", "Continuous"],
    11: [str(i) for i in range(6)],
}
# System screens: menu button -> screen, and their Close/Cancel parts.
SYSTEM_BUTTONS = {
    21: 1600,
    23: 1700,
    25: 2700,
    27: 1800,
    29: 1300,
    31: 1200,
    35: 2000,
    37: 2200,
    45: 2100,
    49: 1100,
    57: 2900,
    102: 3210,
    104: 3219,
}
MENU_CLOSE_PARTS.update(
    {
        1500: 4,
        1501: 6,
        1502: 10,
        1505: 5,
        1507: 8,
        1509: 11,
        1511: 6,
        1516: 11,
        1701: 13,
        1603: 19,
        1204: 18,
        1202: 4,
        2501: 13,
        1600: 14,
        1700: 3,
        2700: 10,
        1800: 24,
        1300: 12,
        1200: 7,
        2000: 11,
        2200: 13,
        2100: 5,
        1100: 7,
        2900: 22,
        3210: 20,
        3219: 5,
    }
)


@dataclass
class FakeZone:
    """A zone holding zones and points."""

    name: str
    # FakeZone | FakeUnit | FakeVentilation | FakeAnalog
    children: list[Any] = field(default_factory=list)


class FakeITM:  # pylint: disable=too-many-instance-attributes
    """The simulated iTM; feed it request bodies with handle()."""

    def __init__(  # noqa: PLR0915  # pylint: disable=too-many-statements
        self, username: str = "user", password: str = "secret"
    ) -> None:
        """Set up three units in Building/Indoor units, one of them stopped."""
        self.username = username
        self.digest = hashlib.sha256(password.encode()).hexdigest()
        self.units = [
            FakeUnit(
                "1:1-00",
                setpoint=21,
                point_id="401",
                address="1-00",
                description="Office",
            ),
            FakeUnit(
                "1:1-04",
                setpoint=23,
                setpoint_range=(19, 20, 21, 22, 23, 24, 25, 26, 27),
                mode="Cool",
                point_id="405",
                address="1-04",
                description="AC 05",
            ),
            FakeUnit(
                "1:1-09",
                running=False,
                setpoint=18,
                setpoint_range=tuple(range(16, 33)),
                mode="Cool",
                modes=("Fan", "Cool", "Heat", "Dry", "Auto"),
                fan_icon=None,
                point_id="210",
                address="1-09",
            ),
        ]
        self.ventilation = [FakeVentilation("Vent 1")]
        self.analog = [FakeAnalog("Alarm 1")]
        self.root = FakeZone(
            "top",
            [
                FakeZone(
                    "Building",
                    [
                        FakeZone("Indoor units", self.units),
                        FakeZone("Ventilation", self.ventilation),
                        FakeZone("Analog", self.analog),
                    ],
                )
            ],
        )
        # A unit error: the main menu bar shows the error indicator and button.
        self.errors: list[list[str]] = []
        # The main menu: what its screens show.
        self.menu_open = False
        self.open_screens: set[int] = set()
        self.version_text = (
            "intelligent Touch Manager\nVer1.00.00\nJan 01 2025, 00:00:00\n"
        )
        self.language = "English"
        self.fahrenheit = False
        self.date_format = 0  # DD/MM/YYYY
        self.time_24h = True
        self.decimal_comma = False
        self.clock_text = "01/01/2025 10:15:30"
        self.dst = False
        self.groups: list[FakeGroup] = []
        self.selected_group: int | None = None
        self.setback = {"high_up": 3, "high_back": 5, "low_up": 1, "low_back": 2}
        self.recovery = {"cool": 3, "heat": 1}
        self.overtime_idx = 1  # 60 min
        self.emgstop: list[tuple[str, bool]] = [("Prog.1", True)]
        self.schedules: list[list[Any]] = [
            [0, "Always", "", ""],
            [4, "Program G", "01/06", "31/08"],
            [3, "Program E", "", ""],
        ]
        self.history_rows = [list(r) for r in HISTORY_ROWS]
        self.history_day = "01/01/2025"
        self.history_loaded_day = self.history_day
        self.history_classes = {3, 4, 5, 6}  # checked class filters
        self.history_job = 0
        self.history_loading = False
        self.history_page = 0
        self.history_page_size = 100
        self.optional_available: dict[int, bool] = {
            400: True,
            500: True,
            600: True,
            900: True,
        }
        self.energy_saving = False
        self.emergency_stop_active = False
        self.naming = False
        self.deleting = False
        self.pending_setback: dict[str, int] = {}
        self.pending_program_enabled: bool | None = None
        self.editor_saving = False
        self.new_program: str | None = None  # the editor's unsaved new program
        self.system_unavailable: set[int] = set()  # system screens that don't open
        self.service_password = "test-service"  # a placeholder, not a real default
        self.service_mode = False
        self.corners: list[int] = []
        # Touch panel settings as option indexes, and the screensaver delay.
        self.hardware = {3: 6, 6: 1, 8: 3, 11: 2}
        self.screensaver = {"mode": 1, "delay": 10, "off_on_error": False}
        self.pending_system: dict[Any, Any] = {}
        self.in_maintenance: set[str] = {self.units[0].name}
        self.point_info = {u.name: f"Unit {u.name}" for u in self.units}
        self.zone_attrs: dict[str, dict[str, Any]] = {
            "Building": {"name": "Building", "info": "", "start": 0, "stop": 0}
        }
        self.web_users = [["admin", "Manager"], ["viewer", "User"]]
        self.language_radio = 2
        self.dst_rule = {9: 3, 10: 0, 11: 0, 12: 1, 14: 8, 15: 1, 16: 0, 17: 1}
        self.passwords: dict[str, str] = {}
        self.network = [
            "intelligent Touch Manager",
            "localhost",
            "192.0.2.20",
            "255.255.255.0",
            "192.0.2.1",
            "9.9.9.9",
            "",
        ]
        self.system_selected: dict[int, Any] = {}
        self.system_dialog: dict[str, Any] = {}
        self.system_confirming: int | None = None
        self.renaming = False
        self.deleting_program = False
        self.pending_recovery: dict[str, int] = {}
        self.pending_overtime: int | None = None
        self.pending_members: list[str] | None = None
        self.member_pick: tuple[int, int] | None = None
        self.pending_settings: dict[str, int] = {}
        self.confirming_settings = False
        # Schedule programs' events by name and weekday (0 = Sunday); the editor.
        office = [
            {
                "time": "06:00",
                "target": "Indoor units",
                "kind": "zone",
                "power": 0,
                "mode_idx": 1,
                "setpoint": 22.0,
                "locks": (2, 1, 0),
                "vent": 0,
            },
            {"time": "18:00", "target": "Indoor units", "kind": "zone", "power": 1},
        ]
        self.schedule_events: dict[str, dict[int, list[dict[str, Any]]]] = {
            "Always": {d: [dict(e) for e in office] for d in range(1, 6)},
        }
        self.special_events: dict[str, dict[str, list[dict[str, Any]]]] = {
            "Always": {
                "Ex1": [
                    {"time": "08:00", "target": "1:1-00", "kind": "unit", "power": 0}
                ]
            },
        }
        self.calendar_rows = [["Jan", "1", "Ex1"]]
        self.selected_program = 0
        self.editor_day = 0
        self.editor_special = 0
        self.editor_list = "weekly"
        self.overview_offset = 0
        self.selected_event: int | None = None
        self.editor_confirming = False
        self.session: str | None = None
        self.tokens: Iterator[str] = (f"{i:032x}" for i in itertools.count(1))
        self.requests: list[dict[str, Any]] = []
        self.fail_next: Exception | None = None
        self.dialog: dict[str, Any] | None = None
        # Changes reach the unit only after this many further logins (the real bus
        # takes a few seconds).
        self.latency_logins = 0
        self.web_version = "Ver1.32.00"
        self._pending: list[list[Any]] = []  # [logins_left, callable, unit]
        # Writes sent while an earlier change to the same unit was still pending.
        self.overlaps = 0
        self.path: list[FakeZone] = [self.root]
        self.selected = 0
        self.list_shown = False  # the list view instead of the icon view
        self.error_shown = False
        # (part, type) -> value the client is believed to show
        self.shown: dict[tuple[int, str], Any] = {}
        self.pending_setpoint: int | None = None
        self.pending_power: int | None = None
        self.confirming_setback: int | None = None

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
            return [("UPDATE", 107, [], [(p, "ENABLED", enabled) for p in (6, 7, 8)])]
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
                (1, "LIST_ROW", rows(["On", "ops@example.com"])),
                (3, "CHECK_STATE", 1),
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
                    (p, "CHECK_STATE", 1 if p in self.history_classes else 0)
                    for p in (3, 4, 5, 6)
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

    def _history_event(self, ev: dict[str, Any]) -> list[Update]:
        part, event, number = ev["part"], ev["event"], ev["number"]
        if event == 5:  # UPD_PROG: the list is loaded on the second request
            if self.history_loading:
                self.history_loading = False
                return [("DLG_UPDATE", 800, [19, 11, 50], [])]
            return [("DLG_CLOSE", 800, [19, 11], [])]
        if part in {3, 4, 5, 6} and event == 1:
            (self.history_classes.add if number else self.history_classes.discard)(part)
            return [("UPDATE", 800, [], [(part, "CHECK_STATE", number)])]
        if part in {9, 10}:
            # Like the real iTM: the day buttons change the date only; the list
            # follows with Refresh (12).
            shown = datetime.strptime(self.history_day, "%d/%m/%Y")  # noqa: DTZ007
            shown += timedelta(days=1 if part == 10 else -1)
            self.history_day = shown.strftime("%d/%m/%Y")
            return [("UPDATE", 800, [], [(8, "TEXT", self.history_day)])]
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
                parts.extend((c, "ENABLED", 0) for c in controls)
            elif check in {37, 94, 129}:
                # Like a real iTM: on the hidden outputs tab, shown but disabled.
                parts.extend((p, "ENABLED", 0) for p in (check, *controls))
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
            enabled = [(c, "ENABLED", number) for c in EXTRA_CONTROLS[part]]
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
            enabled = [(p, "ENABLED", number) for p in (89, 90)]
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


class SystemWrites:
    """One event on a system write screen of the fake.

    Maintenance (2700), point settings (1700/1701), zone settings (1600/1603), web
    users (1200/1204), language (2500/2501) and the time screen (2400). Every OK asks
    "Execute setting?" (dialog 30); only Yes applies.
    """

    def __init__(self, itm: FakeITM, ev: dict[str, Any]) -> None:
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
        return [("UPDATE", self.screen, [], [(p, "ENABLED", 1) for p in enable])]

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
            return [("UPDATE", 2400, [], [(p, "ENABLED", on) for p in itm.dst_rule])]
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


def _rows(cells: list[list[str]]) -> dict[str, Any]:
    """Return a LIST_ROW value for plain text rows."""
    return {
        "rows": [{"text": c, "a": [-1] * len(c), "b": [-1] * len(c)} for c in cells],
        "selected": [],
    }
