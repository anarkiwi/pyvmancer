"""``video status`` interpretation.

Every flag the firmware reports here is advisory: it reports ``connected:
false`` while passing video perfectly, and a locked input while passing nothing
at all. Only observing the output itself proves frames are arriving.
"""

import re
from fractions import Fraction

TIMING_KEYS = ("timing", "output_timing", "out_timing", "format")
INPUT_KEYS = ("input", "source", "input_source")
LOCK_KEYS = ("locked", "lock", "input_locked", "detected")

#: ``video timing`` reports the standards it accepts inside angle brackets.
TIMING_USAGE = re.compile(r"<([^>]*)>")


#: Frame rates of the standards whose names carry no rate; NTSC-family rates are 1000/1001 of nominal.
NAMED_FRAME_RATES = {
    "NTSC": Fraction(30000, 1001),
    "PAL": Fraction(25),
    "480i": Fraction(30000, 1001),
    "480p": Fraction(60000, 1001),
    "576i": Fraction(25),
    "576p": Fraction(50),
}
#: ``<lines><p|i><rate>``, e.g. ``1080p30`` or ``1080i60``; an interlaced rate counts fields.
RATED_TIMING = re.compile(r"^\d+([pi])(\d+(?:\.\d+)?)$")


def frame_period(timing):
    """Seconds per frame of a timing standard name, or ``None`` when the name gives no rate."""
    rate = NAMED_FRAME_RATES.get(str(timing))
    match = RATED_TIMING.match(str(timing))
    if rate is None and match:
        rate = Fraction(match.group(2)) / (2 if match.group(1) == "i" else 1)
    return float(1 / rate) if rate else None


def parse_timings(usage):
    """Timing standards named in a ``video timing`` usage string."""
    match = TIMING_USAGE.search(usage or "")
    if not match:
        return []
    names = (name.strip() for name in match.group(1).split("|"))
    return [name for name in names if name and name != "..."]


def _first(data, keys, default=None):
    """First present key of ``keys`` in ``data``."""
    return next((data[key] for key in keys if key in data), default)


class VideoStatus:
    """Parsed ``video status``: output timing, selected input and lock flags."""

    __slots__ = ("timing", "input_source", "locked", "overridden", "raw")

    def __init__(self, timing, input_source, locked, overridden=False, raw=None):
        self.timing = timing
        self.input_source = input_source
        self.locked = locked
        self.overridden = overridden
        self.raw = dict(raw or {})

    @classmethod
    def from_json(cls, data):
        """Parse a ``video status`` payload, tolerating firmware key drift."""
        flat = {}
        for value in data.values():
            if isinstance(value, dict):
                flat.update(value)
        flat.update({key: value for key, value in data.items() if not isinstance(value, dict)})
        return cls(
            timing=_first(flat, TIMING_KEYS),
            input_source=_first(flat, INPUT_KEYS),
            locked=bool(_first(flat, LOCK_KEYS, False)),
            overridden=bool(flat.get("overridden", False)),
            raw=data,
        )

    @property
    def source_locked(self):
        """True when the selected input's own sub-status reports a signal.

        The top-level flag tracks genlock, so it reads false whenever timing is
        overridden even though the input is fine; the sub-status is authoritative
        in both directions and the top-level flag is only a fallback.
        """
        sub = self.raw.get(str(self.input_source))
        if isinstance(sub, dict) and "locked" in sub:
            return bool(sub["locked"])
        return bool(self.locked)

    @property
    def source_timing(self):
        """Timing the selected input's own sub-status reports, or ``None``."""
        sub = self.raw.get(str(self.input_source))
        return sub.get("timing") if isinstance(sub, dict) else None

    @property
    def output_timing(self):
        """Timing the output is driven at, falling back to the top-level timing."""
        output = self.raw.get("output")
        return output.get("timing", self.timing) if isinstance(output, dict) else self.timing

    def __repr__(self):
        return (
            f"VideoStatus(timing={self.timing!r}, input={self.input_source!r}, "
            f"locked={self.locked}, overridden={self.overridden})"
        )
