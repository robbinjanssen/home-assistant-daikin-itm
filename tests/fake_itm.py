"""A small simulated iTM that behaves like the real one where it matters.

- one web session at a time; logins are rejected while another session is active
- replies only contain operation-panel parts that differ from what the client is
  believed to show, and a client's own selection is never echoed back
- setpoint changes are applied on the TIMER commit, and ignored while a unit is
  stopped
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from daikin_itm.protocol import PART_UPDATE_CODE, SCREEN_UPDATE_CODE, Reader, Writer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

POWER_OPTIONS = ["Start", "SB Laag", "SB Hoog", "Stop"]

# (update type, screen, data, [(part, part type, value)])
Update = tuple[str, int, list[Any], list[tuple[int, str, Any]]]


@dataclass
class FakeUnit:
    """An indoor unit."""

    name: str
    running: bool = True
    setpoint: int = 21
    setpoint_range: tuple[int, ...] = (19, 20, 21, 22, 23, 24)
    temp: float = 21.0
    mode: str = "Verwarmen"
    modes: tuple[str, ...] = ("Ventilator", "Koelen", "Verwarmen")
    fan_icon: int | None = 3011  # None: no fan speed control
    point_id: str = "100"
    address: str = "1-00"
    description: str = ""


@dataclass
class FakeZone:
    """A zone holding zones and units."""

    name: str
    children: list[Any] = field(default_factory=list)  # FakeZone | FakeUnit


class FakeITM:  # pylint: disable=too-many-instance-attributes
    """The simulated iTM; feed it request bodies with handle()."""

    def __init__(self, username: str = "user", password: str = "secret") -> None:
        """Set up three units in Building/Indoor units, one of them stopped."""
        self.username = username
        self.digest = hashlib.sha256(password.encode()).hexdigest()
        self.units = [
            FakeUnit(
                "1:1-00",
                setpoint=21,
                point_id="101",
                address="1-00",
                description="Office",
            ),
            FakeUnit(
                "1:1-04",
                setpoint=20,
                setpoint_range=(20, 21, 22, 23, 24, 25),
                mode="Koelen",
                point_id="105",
                address="1-04",
                description="Meeting room",
            ),
            FakeUnit(
                "1:1-09",
                running=False,
                setpoint=16,
                setpoint_range=tuple(range(16, 33)),
                mode="Koelen",
                modes=("Ventilator", "Koelen", "Verwarmen", "Drogen", "Auto"),
                fan_icon=None,
                point_id="110",
                address="1-09",
            ),
        ]
        self.root = FakeZone(
            "top",
            [
                FakeZone(
                    "Building",
                    [FakeZone("Indoor units", self.units), FakeZone("Ventilation")],
                )
            ],
        )
        self.session: str | None = None  # active token
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
        # (part, type) -> value the client is believed to show
        self.shown: dict[tuple[int, str], Any] = {}
        self.pending_setpoint: int | None = None
        self.pending_power: int | None = None

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
            return self._reply(None, [("OPEN", 1203, [-1], [(0, "LABEL", "Gebr")])])
        if ev["screen"] == 1203 and ev["event"] == 1:
            return self._login(ev["string"])
        if self.session is None or token != self.session:
            return self._reply(None, [("LOGOUT", -1, [3], [])])
        if ev["event"] == 10:  # logout
            self.session = None
            return self._reply(None, [])
        return self._reply(self.session, self._event(ev))

    def _login(self, s: str) -> str:
        n = int(s[:4])
        user, digest = s[4 : 4 + n], s[4 + n :]
        if self.session is not None or user != self.username or digest != self.digest:
            return self._reply(
                None,
                [("DLG_OPEN", 1203, [6, 1, "Kan niet aanmelden", "Fout", "OK"], [])],
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
        return self._reply(
            self.session,
            [
                ("LOGIN", -1, [102, "Informatie", ""], []),
                ("OPEN", 1, [-1], [(6, "ICONPANEL", self._icons())]),
                ("OPEN", 4, [-1], self._panel_delta()),
            ],
        )

    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        routes: list[tuple[bool, Callable[[dict[str, Any]], list[Update]]]] = [
            (screen == 1 and part == 6 and event == 1, self._select_icon),
            (screen == 1 and part == 2 and event == 1, self._zone_down),
            (screen == 4 and event == 1 and part in {9, 11}, self._choose),
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
        else:
            self.pending_power = ev["number"]
        # The client already shows its own pick.
        self.shown[ev["part"], "SINGLE_SELIDX"] = ev["number"]
        return [("UPDATE", 4, [], [])]

    def _details(self, _ev: dict[str, Any]) -> list[Update]:
        u = self._item()
        chars = (
            "Zone:\nFloor 1&gt;Building&gt;Indoor units\n\nThermostaatstatus [UIT]\n"
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
        item = self._item()
        power, setpoint = self.pending_power, self.pending_setpoint

        def change(item: FakeUnit = item) -> None:
            if power is not None:
                item.running = power == 0
            if setpoint is not None and item.running:
                item.setpoint = setpoint

        self._later(change)
        self.pending_setpoint = self.pending_power = None
        return []

    # -- settings dialog (screen 7) -------------------------------------------------
    def _open_dialog(self, _ev: dict[str, Any]) -> list[Update]:
        self._check_overlap()
        u = self._item()
        self.dialog = {"ticks": set(), "power": None, "mode": None, "fan": u.fan_icon}
        parts = [
            (0, "LABEL", "Gedetailleerde instellingen : " + u.name),
            (2, "LABEL", "Aan/uit"),
            (3, "ENABLED", 0),
            (3, "CHECK_STATE", 1 if u.running else 0),
            (4, "ENABLED", 0),
            (4, "CHECK_STATE", 0 if u.running else 1),
            (17, "LABEL", "Bedieningsmodus"),
            (18, "ENABLED", 0),
            (18, "COMBO_STRS", list(u.modes)),
            (18, "SINGLE_SELIDX", u.modes.index(u.mode)),
            (28, "LABEL", "Ventilatie"),
            (28, "VISIBLED", 1 if u.fan_icon else 0),
            (31, "ICONID", u.fan_icon or 3011),
        ]
        return [("OPEN", 7, [0], parts)]

    def _dialog_event(self, ev: dict[str, Any]) -> list[Update]:
        part, event, number = ev["part"], ev["event"], ev["number"]
        d, u = self.dialog, self._item()
        updates: list[Update] = [("UPDATE", 7, [], [])]
        if d is None:
            updates = []
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
            confirm = [87, 2, "Instelling uitvoeren?", "Bevestigen", "Ja", "Nee"]
            updates = [("DLG_OPEN", 7, confirm, [])]
        elif part == 87 and d.get("confirming") and event == 2:  # "Nee": back
            d["confirming"] = False
            updates = [("DLG_CLOSE", 7, [87, 2], [])]
        elif (part == 87 and d.get("confirming") and event == 1) or part == 6:
            # "Ja" applies the settings, cancel (6) drops them.
            if part == 87:
                self._later(self._dialog_change(u, dict(d)))
            self.dialog = None
            updates = [("CLOSE", 7, [], []), ("UPDATE", 4, [], self._panel_delta())]
        return updates

    @staticmethod
    def _dialog_change(u: FakeUnit, d: dict[str, Any]) -> Callable[[], None]:
        def change() -> None:
            if d["power"] is not None:
                u.running = d["power"]
            if d["mode"] is not None:
                u.mode = d["mode"]
            if 28 in d["ticks"] and d["fan"]:
                u.fan_icon = d["fan"]

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
        return cast("FakeUnit", self.path[-1].children[self.selected])

    def _icons(self) -> dict[str, Any]:
        icons = []
        for c in self.path[-1].children:
            if isinstance(c, FakeZone):
                icons.append([c.name, 1000, 0])
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
                (1, "ICONID_MODE"): [2114, 0 if item.running else 2],
                (4, "LABEL"): f"{item.temp:.1f}°C",
                (5, "LABEL"): item.mode,
                (9, "COMBO_STRS"): POWER_OPTIONS,
                (9, "SINGLE_SELIDX"): 0 if item.running else 3,
                (11, "COMBO_NUMS"): list(item.setpoint_range),
                (11, "SINGLE_SELIDX"): item.setpoint_range.index(item.setpoint),
                (14, "ICONID"): item.fan_icon or 3011,
                (2, "VISIBLED"): 0,
                (19, "VISIBLED"): 0,
                (4, "VISIBLED"): 1,
                (5, "VISIBLED"): 1,
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


def _encode_data(w: Writer, kind: str, data: list[Any]) -> None:
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
    elif kind == "DLG_OPEN":
        w.short(data[0])
        w.short(data[1])
        for s in data[2:]:
            w.string(s)


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
    elif ptype == "ICONPANEL":
        w.int(len(v["icons"]))
        for name, a, b in v["icons"]:
            w.string(name)
            w.short(a)
            w.short(b)
        w.int(v["selected"])
    else:
        raise NotImplementedError(ptype)
