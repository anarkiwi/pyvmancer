"""Videomancer protocol constants.

Sourced from the LZX Industries Videomancer user manual, modulation guide and
serial command guide. See ``docs/protocol.md`` for provenance of each table.
"""

from enum import Enum, IntEnum

#: Package version. Lives here so leaf modules can read it without importing the package.
VERSION = "0.2.1"

USB_VID = 0x16D0
USB_PID = 0x14DB
USB_MANUFACTURER = "LZX Industries"
USB_PRODUCT = "Videomancer"

PARAM_COUNT = 12
PARAM_MIN = 0
PARAM_MAX = 1023
PARAM_CENTER = 512
#: A toggle reads as "on" when its combined value is >= this midpoint.
BOOL_THRESHOLD = 512

#: Per-parameter values on a freshly loaded program.
PARAM_DEFAULTS = (512, 512, 512, 512, 512, 512, 0, 0, 0, 0, 0, 512)

#: Default CC (MSB) for parameters P1..P12.
DEFAULT_CC_MSB = tuple(range(PARAM_COUNT))
#: 14-bit CC pairing offset: the LSB controller is always MSB + 32.
CC_LSB_OFFSET = 32
CC_MIN = 0
CC_MAX = 127

#: MIDI notes 0..11 trigger modulators P1..P12.
NOTE_BASE = 0

MIDI_CLOCK_PPQN = 24
BPM_MIN = 10.0
BPM_MAX = 400.0
#: ``transport bpm`` takes hundredths of a BPM.
BPM_SCALE = 100

SHELL_MAX_LINE = 511
SHELL_OK = "ok"
SHELL_PATH_PREFIX = "sd:/"


class ParamKind(Enum):
    """Physical control backing each parameter slot."""

    KNOB = "knob"
    TOGGLE = "toggle"
    FADER = "fader"


def param_kind(param):
    """Return the :class:`ParamKind` for a 1-based parameter number."""
    if not 1 <= param <= PARAM_COUNT:
        raise ValueError(f"parameter must be 1..{PARAM_COUNT}, got {param}")
    if param <= 6:
        return ParamKind.KNOB
    if param <= 11:
        return ParamKind.TOGGLE
    return ParamKind.FADER


#: Parameters that render per-scanline when driven by a per-line operator.
PER_LINE_PARAMS = frozenset({1, 2, 3, 4, 5, 6, 12})

#: P12 is the crossfader and gates the output even where a program leaves it unnamed.
CROSSFADER_PARAM = 12

#: Manual reference that makes a MIDI CC absolute: zero, with the crossfader open.
PARK_REFERENCE = tuple(
    PARAM_MAX if param == CROSSFADER_PARAM else PARAM_MIN for param in range(1, PARAM_COUNT + 1)
)

#: Names ``program info`` uses for a slot the program does not assign.
UNASSIGNED_NAMES = frozenset({"", "-", "null", "none"})

#: Default sweep resolution; a native range no wider than this is enumerable instead.
SWEEP_STEPS = 32


class ParamRole(Enum):
    """How a program's declared parameter range should be sampled."""

    CONTINUOUS = "continuous"
    BOOLEAN = "boolean"
    QUANTIZED = "quantized"
    UNASSIGNED = "unassigned"


def is_unassigned(name):
    """True when ``program info`` names a slot ``-`` or ``Null <n>``."""
    text = str(name).strip().lower()
    return text in UNASSIGNED_NAMES or (text.startswith("null") and text[4:].strip().isdigit())


def classify_param(name, minimum, maximum, sweep_steps=SWEEP_STEPS):
    """Classify one ``program info`` entry as ``(role, steps)``.

    ``0..1`` is a boolean resolving on at :data:`BOOL_THRESHOLD`; an integer
    range with no more positions than ``sweep_steps`` quantises to exactly those
    positions; ``steps`` is None where the role does not bound sampling.
    """
    low, high = float(minimum), float(maximum)
    if is_unassigned(name) or high <= low:
        return ParamRole.UNASSIGNED, None
    if (low, high) == (0.0, 1.0):
        return ParamRole.BOOLEAN, 2
    span = high - low
    if low.is_integer() and span.is_integer() and span + 1 <= sweep_steps:
        return ParamRole.QUANTIZED, int(span) + 1
    return ParamRole.CONTINUOUS, None


class Operator(IntEnum):
    """Modulation operators, by the numeric id used in preset ``sr:`` fields.

    Ids 23..28 are not documented and are intentionally absent; resolve those
    from the device with ``program state`` rather than guessing.
    """

    DISABLED = 0
    FREE_LFO = 1
    RANDOM = 2
    TURING_MACHINE = 3
    BOUNCING_BALL = 4
    LOGISTIC_MAP = 5
    EUCLIDEAN_RHYTHM = 6
    SYNC_LFO = 7
    AUDIO_INPUT = 8
    COMPARATOR = 9
    PENDULUM = 10
    DRIFT = 11
    RING_MOD = 12
    CELLULAR = 13
    PULSE_WIDTH = 14
    PEAK_HOLD = 15
    FIELD_ACCUM = 16
    SLEW_LIMITER = 17
    PERLIN_NOISE = 18
    WAVEFOLDER = 19
    CLOCK_DIV = 20
    PROB_GATE = 21
    QUANTIZER = 22
    MIDI_TURING = 29
    CV_INPUT = 30


class OperatorCategory(Enum):
    """Grouping used by the modulation guide."""

    NONE = "none"
    OSCILLATORS = "oscillators"
    RANDOM_CHAOS = "random & chaos"
    EXTERNAL_INPUT = "external input"
    PHYSICS = "physics"
    SEQUENCING = "sequencing & rhythm"


class Transport(Enum):
    """Operator clocking: free-running, or gated by transport playback."""

    FREE_RUN = "free-run"
    TEMPO = "tempo"


_OPS = (
    # (operator, display glyph, category, per-line capable, transport)
    (Operator.DISABLED, "·", OperatorCategory.NONE, False, Transport.FREE_RUN),
    (Operator.FREE_LFO, "L", OperatorCategory.OSCILLATORS, False, Transport.FREE_RUN),
    (Operator.RANDOM, "R", OperatorCategory.RANDOM_CHAOS, False, Transport.FREE_RUN),
    (Operator.TURING_MACHINE, "U", OperatorCategory.RANDOM_CHAOS, False, Transport.FREE_RUN),
    (Operator.BOUNCING_BALL, "B", OperatorCategory.PHYSICS, False, Transport.FREE_RUN),
    (Operator.LOGISTIC_MAP, "X", OperatorCategory.RANDOM_CHAOS, False, Transport.FREE_RUN),
    (Operator.EUCLIDEAN_RHYTHM, "Y", OperatorCategory.SEQUENCING, False, Transport.FREE_RUN),
    (Operator.SYNC_LFO, "M", OperatorCategory.OSCILLATORS, False, Transport.TEMPO),
    (Operator.AUDIO_INPUT, "A", OperatorCategory.EXTERNAL_INPUT, True, Transport.FREE_RUN),
    (Operator.COMPARATOR, "K", OperatorCategory.EXTERNAL_INPUT, True, Transport.FREE_RUN),
    (Operator.PENDULUM, "N", OperatorCategory.PHYSICS, False, Transport.FREE_RUN),
    (Operator.DRIFT, "W", OperatorCategory.RANDOM_CHAOS, False, Transport.FREE_RUN),
    (Operator.RING_MOD, "*", OperatorCategory.EXTERNAL_INPUT, True, Transport.FREE_RUN),
    (Operator.CELLULAR, "#", OperatorCategory.RANDOM_CHAOS, False, Transport.FREE_RUN),
    (Operator.PULSE_WIDTH, "P", OperatorCategory.OSCILLATORS, False, Transport.FREE_RUN),
    (Operator.PEAK_HOLD, "J", OperatorCategory.EXTERNAL_INPUT, True, Transport.FREE_RUN),
    (Operator.FIELD_ACCUM, "I", OperatorCategory.EXTERNAL_INPUT, False, Transport.FREE_RUN),
    (Operator.SLEW_LIMITER, "/", OperatorCategory.EXTERNAL_INPUT, False, Transport.FREE_RUN),
    (Operator.PERLIN_NOISE, "~", OperatorCategory.RANDOM_CHAOS, False, Transport.FREE_RUN),
    (Operator.WAVEFOLDER, "Z", OperatorCategory.OSCILLATORS, False, Transport.FREE_RUN),
    (Operator.CLOCK_DIV, "V", OperatorCategory.SEQUENCING, False, Transport.TEMPO),
    (Operator.PROB_GATE, "p", OperatorCategory.SEQUENCING, False, Transport.FREE_RUN),
    (Operator.QUANTIZER, "O", OperatorCategory.EXTERNAL_INPUT, True, Transport.FREE_RUN),
    (Operator.MIDI_TURING, "T", OperatorCategory.RANDOM_CHAOS, False, Transport.FREE_RUN),
    (Operator.CV_INPUT, "C", OperatorCategory.EXTERNAL_INPUT, True, Transport.FREE_RUN),
)


#: Words that keep their casing in operator display names.
_ACRONYMS = {"LFO": "LFO", "CV": "CV", "MIDI": "MIDI", "ACCUM": "Accum", "PROB": "Prob"}


class OperatorInfo:
    """Static metadata for one modulation operator."""

    __slots__ = ("operator", "glyph", "category", "per_line", "transport")

    def __init__(self, operator, glyph, category, per_line, transport):
        self.operator = operator
        self.glyph = glyph
        self.category = category
        self.per_line = per_line
        self.transport = transport

    @property
    def name(self):
        """Human-readable operator name, e.g. ``"Free LFO"``."""
        words = self.operator.name.split("_")
        return " ".join(_ACRONYMS.get(word, word.title()) for word in words)

    def __repr__(self):
        return f"OperatorInfo({self.name!r}, id={int(self.operator)})"


OPERATORS = {row[0]: OperatorInfo(*row) for row in _OPS}

_OPERATOR_BY_NAME = {}
for _info in OPERATORS.values():
    _OPERATOR_BY_NAME[_info.operator.name.lower()] = _info.operator
    _OPERATOR_BY_NAME[_info.name.lower()] = _info.operator


def resolve_operator(value):
    """Coerce an int, :class:`Operator` or name string to an :class:`Operator`."""
    if isinstance(value, Operator):
        return value
    if isinstance(value, int):
        return Operator(value)
    key = str(value).strip().lower().replace("-", " ")
    try:
        return _OPERATOR_BY_NAME[key]
    except KeyError:
        raise ValueError(f"unknown operator {value!r}") from None


#: Operators triggered by MIDI note-on for their parameter slot.
NOTE_TRIGGERED = frozenset({Operator.BOUNCING_BALL, Operator.PENDULUM, Operator.MIDI_TURING})

#: Modulation knobs shared by every operator.
MOD_KNOBS = ("time", "space", "slope")

#: Preset payload field names used by ``program presets save``.
PRESET_FIELDS = {
    "manual": "m",
    "time": "t",
    "space": "sp",
    "slope": "sl",
    "source": "sr",
}


class PresetBank(Enum):
    """Preset storage bank."""

    FACTORY = "factory"
    USER = "user"


class TransportState(Enum):
    """Values reported by ``transport status``."""

    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class LogLevel(Enum):
    """Argument to the ``log level`` command."""

    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    NONE = "none"


class ShellErrorCode(IntEnum):
    """Numeric error codes returned as ``!<code>:<message>``."""

    UNKNOWN_COMMAND = 1
    PARSE_ERROR = 2
    SERVICE_UNAVAILABLE = 3
    BUFFER_OVERFLOW = 4
    NOT_CONNECTED = 5
    FILE_NOT_FOUND = 6
    IO_ERROR = 7
    SD_NOT_MOUNTED = 8
    PATH_TOO_LONG = 9
    INVALID_PATH = 10


#: Firmware-embedded programs; query ``programs list`` for the full SD-backed set.
EMBEDDED_PROGRAMS = (
    "bitcullis",
    "corollas",
    "delirium",
    "elastica",
    "faultplane",
    "fauxtress",
    "glorious",
    "howler",
    "isotherm",
    "kintsugi",
    "lumarian",
    "moire",
    "mycelium",
    "perlin",
    "pinwheel",
    "pong",
    "sabattier",
    "scramble",
    "shadebob",
    "stic",
    "stipple",
    "yuv_amplifier",
    "yuv_phaser",
)
