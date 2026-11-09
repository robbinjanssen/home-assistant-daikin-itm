"""The simulated iTM's shared data and state."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

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


class FakeITMBase:  # pylint: disable=too-many-instance-attributes
    """The simulated iTM's state, shared by its screens."""

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


def _enabled(parts: Iterable[int], on: int) -> list[tuple[int, str, Any]]:
    """Return ENABLED updates for parts, all on or all off."""
    return [(p, "ENABLED", int(on)) for p in parts]


def _rows(cells: list[list[str]]) -> dict[str, Any]:
    """Return a LIST_ROW value for plain text rows."""
    return {
        "rows": [{"text": c, "a": [-1] * len(c), "b": [-1] * len(c)} for c in cells],
        "selected": [],
    }
