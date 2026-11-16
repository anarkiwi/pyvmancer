"""MIDI CC map: compare the device's assignments with the CCs a controller sends and assign them.

``modulation cc-map`` lists each slot's MSB/LSB pair (LSB 255 when none), or ``null`` when
unassigned. Settings key ``CC_MAP_SETTING_KEY + slot`` persists it as ``CC_MAP_ASSIGNED | msb``
or ``CC_MAP_UNASSIGNED``; nothing is written unless every key on the device fits that.
"""

from collections import defaultdict
from typing import NamedTuple

from .const import (
    ASSIGN_SINK_CCS,
    CC_MAP_ASSIGNED,
    CC_MAX,
    CC_MSB_MASK,
    CC_MAP_SETTING_KEY,
    CC_MAP_UNASSIGNED,
    PARAM_COUNT,
)
from .errors import CcMapError, VmancerError
from .midi import MidiController, lsb_partner
from .shell import read_setting

PARAMS = tuple(range(1, PARAM_COUNT + 1))


def expected_cc_map(midi=None):
    """``(msb, lsb)`` per slot that ``midi`` sends, ``lsb`` ``None`` where it sends no LSB."""
    midi = midi or MidiController(None)
    return tuple(
        (int(msb), lsb_partner(int(msb)) if midi.resolution(param) == 14 else None)
        for param, msb in zip(PARAMS, midi.cc_map)
    )


def sink_cc(midi=None):
    """The first of ``ASSIGN_SINK_CCS`` that ``midi`` never sends, MSB or LSB."""
    return sink_cc_of(expected_cc_map(midi))


def sink_cc_of(expected):
    """The first of ``ASSIGN_SINK_CCS`` outside an expected ``(msb, lsb)`` map."""
    sent = {cc for pair in expected for cc in pair if cc is not None}
    free = [cc for cc in ASSIGN_SINK_CCS if cc not in sent]
    if not free:
        raise ValueError(
            f"the CC map uses every undefined controller {ASSIGN_SINK_CCS}; none is free to sink"
        )
    return free[0]


def parse_cc_map(raw):
    """``(msb, lsb)`` or ``None`` per slot from a ``modulation cc-map`` reply, or ``None`` if absent."""
    slots = raw.get("assignments") if isinstance(raw, dict) else None
    if not isinstance(slots, list) or len(slots) != PARAM_COUNT:
        return None
    return tuple(None if slot is None else (int(slot["msb"]), _lsb(slot.get("lsb"))) for slot in slots)


def _lsb(value):
    """An LSB controller number, or ``None`` for the device's no-LSB marker."""
    return None if value is None or not 0 <= int(value) <= CC_MAX else int(value)


def encode_setting(msb):
    """Settings value assigning ``msb``, or leaving the slot unassigned for ``None``."""
    return CC_MAP_UNASSIGNED if msb is None else CC_MAP_ASSIGNED | int(msb)


class CcMapReport(NamedTuple):
    """The device's ``(msb, lsb)`` or ``None`` per slot against the expected ``(msb, lsb)``."""

    expected: tuple
    actual: tuple

    @property
    def unassigned(self):
        """Params the device leaves without a CC."""
        return tuple(p for p, slot in zip(PARAMS, self.actual) if slot is None)

    @property
    def mismatched(self):
        """Params assigned a CC other than the expected MSB, or LSB where one is sent."""
        return tuple(
            p
            for p, (msb, lsb), slot in zip(PARAMS, self.expected, self.actual)
            if slot is not None and (slot[0] != msb or (lsb is not None and slot[1] != lsb))
        )

    @property
    def shared(self):
        """``{param: other params}`` for device slots listening on a sent CC another slot also listens on."""
        sent = {cc for pair in self.expected for cc in pair if cc is not None}
        owners = defaultdict(set)
        for param, slot in zip(PARAMS, self.actual):
            for cc in sent.intersection(slot or ()):
                owners[cc].add(param)
        shared = defaultdict(set)
        for params in owners.values():
            for param in params:
                shared[param] |= params - {param}
        return {p: tuple(sorted(o)) for p, o in sorted(shared.items()) if o}

    @property
    def wrong(self):
        """Params whose setting must be rewritten."""
        return tuple(sorted(set(self.unassigned) | set(self.mismatched)))

    @property
    def ok(self):
        """True when every slot is on its expected CC and no two slots share one."""
        return not (self.wrong or self.shared)

    def writes(self):
        """``(key, value)`` settings writes assigning the expected map to every slot."""
        return tuple(
            (CC_MAP_SETTING_KEY + i, encode_setting(msb)) for i, (msb, _) in enumerate(self.expected)
        )

    @property
    def armed(self):
        """Params that captured the sink CC: MIDI Assign was armed on them."""
        sink = sink_cc_of(self.expected)
        return tuple(p for p, slot in zip(PARAMS, self.actual) if slot is not None and slot[0] == sink)

    def summary(self):
        """One line naming what each wrong or shared slot was on, e.g. ``P12 CC0``, or ``ok``."""
        slots = sorted(set(self.wrong) | set(self.shared))
        return ", ".join(f"P{p} {_name(self.actual[p - 1])}" for p in slots) or "ok"

    def as_dict(self):
        """JSON-ready comparison."""
        return {
            "ok": self.ok,
            "expected": [{"msb": msb, "lsb": lsb} for msb, lsb in self.expected],
            "actual": [None if s is None else {"msb": s[0], "lsb": s[1]} for s in self.actual],
            "mismatched": list(self.mismatched),
            "shared": {str(p): list(o) for p, o in self.shared.items()},
            "unassigned": list(self.unassigned),
            "armed": list(self.armed),
        }


def _name(slot):
    """``CC<msb>``, or ``unassigned``."""
    return "unassigned" if slot is None else f"CC{slot[0]}"


def _span(values):
    """``0..11`` for a consecutive run, else a comma list."""
    values = list(values)
    if values == list(range(values[0], values[0] + len(values))):
        return f"{values[0]}..{values[-1]}"
    return ",".join(map(str, values))


def cc_map_report(shell, midi=None):
    """Compare ``shell.cc_map()`` with what ``midi`` (default: a default controller) sends, or ``None``."""
    actual = parse_cc_map(shell.cc_map())
    return None if actual is None else CcMapReport(expected_cc_map(midi), actual)


def _require_report(shell, midi):
    """:func:`cc_map_report`, raising when the firmware reports no map."""
    report = cc_map_report(shell, midi)
    if report is None:
        raise VmancerError("device reported no 12-slot CC map")
    return report


def read_cc_settings(shell):
    """The 12 CC-map settings values, as ``settings get`` reports them."""
    return tuple(
        read_setting(shell, key) for key in range(CC_MAP_SETTING_KEY, CC_MAP_SETTING_KEY + PARAM_COUNT)
    )


def check_encoding(values):
    """Raise unless every setting is ``CC_MAP_UNASSIGNED`` or ``CC_MAP_ASSIGNED | msb``."""
    for key, value in enumerate(values, start=CC_MAP_SETTING_KEY):
        if value != CC_MAP_UNASSIGNED and value & ~CC_MSB_MASK != CC_MAP_ASSIGNED:
            raise VmancerError(
                f"settings key {key} holds {value:#x}, outside the known CC-map encoding "
                f"({CC_MAP_ASSIGNED:#x} | msb, {CC_MAP_UNASSIGNED:#x} unassigned); refusing to write"
            )


class CcMapAssignment(NamedTuple):
    """An assignment: the map before, ``(key, old, new)`` settings writes, and the map after when applied."""

    before: CcMapReport
    writes: tuple
    after: CcMapReport | None

    def summary(self):
        """One log line, e.g. ``cc-map: assigned P1..P12 -> CC0..11 (was: P12 CC0)``."""
        msbs = [msb for msb, _ in self.before.expected]
        verb = "assigned" if self.after is not None else "would assign"
        armed = self.before.armed
        note = f"; MIDI Assign was armed on P{', P'.join(map(str, armed))}" if armed else ""
        return f"cc-map: {verb} P1..P{len(msbs)} -> CC{_span(msbs)} (was: {self.before.summary()}){note}"

    def as_dict(self):
        """JSON-ready summary."""
        return {
            "dry_run": self.after is None,
            "summary": self.summary(),
            "before": self.before.as_dict(),
            "writes": [{"key": k, "old": old, "new": new} for k, old, new in self.writes],
            "commands": [
                f"settings set {k} {v}" for k, _, new in self.writes for v in (CC_MAP_UNASSIGNED, new)
            ],
            "after": None if self.after is None else self.after.as_dict(),
        }


def verify_cc_map(shell, midi=None):
    """Read the device map back and raise :class:`CcMapError` unless it matches what ``midi`` sends."""
    report = _require_report(shell, midi)
    if not report.ok:
        raise CcMapError(f"device CC map differs from the CCs sent ({report.summary()}): {report.as_dict()}")
    return report


def reassert_cc_map(shell, midi=None):
    """Verify the map, reassigning when the only change is the sink CC captured by an armed MIDI Assign.

    Returns the :class:`CcMapAssignment` when one was needed, else ``None``; any other change raises.
    """
    report = _require_report(shell, midi)
    if report.ok:
        return None
    if set(report.wrong) | set(report.shared) != set(report.armed):
        raise CcMapError(f"device CC map changed ({report.summary()}): {report.as_dict()}")
    return assign_cc_map(shell, midi)


def assign_cc_map(shell, midi=None, apply=True):
    """Write every slot's expected CC to the device, then verify it; ``apply=False`` only plans.

    A key can already hold its target while the live slot differs, and rewriting an unchanged
    value is ignored, so each key is cleared to unassigned and then written. Refuses on an unknown
    encoding; raises :class:`CcMapError` if the map does not take.
    """
    before = _require_report(shell, midi)
    values = read_cc_settings(shell)
    check_encoding(values)
    writes = tuple((key, values[key - CC_MAP_SETTING_KEY], new) for key, new in before.writes())
    if not apply:
        return CcMapAssignment(before, writes, None)
    for key, _, new in writes:
        shell.set_setting(key, CC_MAP_UNASSIGNED)
        shell.set_setting(key, new)
    return CcMapAssignment(before, writes, verify_cc_map(shell, midi))
