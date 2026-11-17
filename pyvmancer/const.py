"""Videomancer protocol constants.

Sourced from the LZX Industries Videomancer user manual, modulation guide and
serial command guide. See ``docs/protocol.md`` for provenance of each table.
"""

from enum import Enum, IntEnum

#: Package version. Lives here so leaf modules can read it without importing the package.
VERSION = "0.6.5"

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


class SettingKey(IntEnum):
    """Numeric ``settings get``/``set`` keys from the serial port guide's Setting Keys table."""

    VIDEO_ROUTE_MODE = 1
    ANALOG_INPUT_MODE = 2
    ANALOG_OUTPUT_MODE = 3
    MIDI_CHANNEL = 4
    DEVELOPER_MODE = 5
    LANGUAGE = 6
    CURRENT_PROGRAM = 7
    PREFERRED_TIMING = 8
    BOOT_COUNT = 9
    SELECTED_SLOT = 10
    TRANSPORT_BPM = 11
    HDMI_OUTPUT_MODE = 12
    SOFT_PICKUP = 13
    MIDI_OUTPUT_MODE = 14
    ENC_TEST_PATT = 15
    ENC_ED_HD_LVL = 16
    H_PHASE_OUT = 17
    H_PHASE_IN = 18
    PROGRAM_AUTHOR = 19


#: Panel names of the named settings keys, as the guide gives them.
SETTING_NAMES = {
    SettingKey.VIDEO_ROUTE_MODE: "Video Route Mode",
    SettingKey.ANALOG_INPUT_MODE: "Analog Input Mode",
    SettingKey.ANALOG_OUTPUT_MODE: "Analog Output Mode",
    SettingKey.MIDI_CHANNEL: "MIDI Channel",
    SettingKey.DEVELOPER_MODE: "Developer Mode",
    SettingKey.LANGUAGE: "Language",
    SettingKey.CURRENT_PROGRAM: "current program",
    SettingKey.PREFERRED_TIMING: "Preferred Timing",
    SettingKey.BOOT_COUNT: "boot count",
    SettingKey.SELECTED_SLOT: "selected slot",
    SettingKey.TRANSPORT_BPM: "transport BPM x100",
    SettingKey.HDMI_OUTPUT_MODE: "HDMI Output Mode",
    SettingKey.SOFT_PICKUP: "Soft Pickup",
    SettingKey.MIDI_OUTPUT_MODE: "MIDI Output Mode",
    SettingKey.ENC_TEST_PATT: "Enc Test Patt",
    SettingKey.ENC_ED_HD_LVL: "Enc ED/HD Lvl",
    SettingKey.H_PHASE_OUT: "H Phase Out",
    SettingKey.H_PHASE_IN: "H Phase In",
    SettingKey.PROGRAM_AUTHOR: "program author",
}
#: Vid Route Mode values by panel name.
ROUTE_MODES = dict(enumerate(("Analog In", "HDMI In", "Dual In", "Standalone", "Auto")))
#: Preferred Timing codes: the FPGA's 4-bit timing value, not a list position.
TIMING_CODES = dict(
    enumerate(
        (
            "NTSC",
            "1080i50",
            "1080i5994",
            "1080p24",
            "480p",
            "720p50",
            "720p5994",
            "1080p30",
            "PAL",
            "1080p2398",
            "1080i60",
            "1080p25",
            "576p",
            "1080p2997",
            "720p60",
        )
    )
)
_DISABLED_ENABLED = ("Disabled", "Enabled")
#: Value names of the enumerated settings keys.
SETTING_VALUES = {
    SettingKey.VIDEO_ROUTE_MODE: ROUTE_MODES,
    SettingKey.ANALOG_INPUT_MODE: dict(
        enumerate(("Composite", "S-Video", "YPbPr", "RGB SOG", "RGB 1V", "YPbPr 1V", "YYY 1V", "Auto"))
    ),
    SettingKey.ANALOG_OUTPUT_MODE: dict(enumerate(("CVBS/S-Video", "YPbPr", "RGsB", "YYY", "SOG"))),
    SettingKey.MIDI_CHANNEL: {0: "Disabled", 1: "Omni", **{n + 1: f"channel {n}" for n in range(1, 17)}},
    SettingKey.DEVELOPER_MODE: dict(enumerate(_DISABLED_ENABLED)),
    SettingKey.PREFERRED_TIMING: TIMING_CODES,
    SettingKey.HDMI_OUTPUT_MODE: dict(enumerate(("HDMI", "DVI"))),
    SettingKey.SOFT_PICKUP: dict(enumerate(_DISABLED_ENABLED)),
    SettingKey.MIDI_OUTPUT_MODE: dict(enumerate(("Disabled", "Manual+Mod", "Manual Only", "Mod Only"))),
    SettingKey.ENC_TEST_PATT: dict(enumerate(("Off", "On"))),
    SettingKey.ENC_ED_HD_LVL: dict(enumerate(("Studio", "Full"))),
}
#: Settings key of channel 0's saved modulator state: ``MODULATOR_SETTING_KEY + channel * 5 + field``.
MODULATOR_SETTING_KEY = 100
#: Saved modulator fields, in key order within a channel.
MODULATOR_FIELDS = ("manual", "time", "space", "slope", "source")
#: Settings key holding P1's MIDI Assign; P2..P12 follow consecutively.
CC_MAP_SETTING_KEY = 200
#: MIDI Assign packing: bits 0-7 the CC, bits 8-15 the LSB CC, bit 16 auto-pair.
CC_MSB_MASK = 0xFF
CC_LSB_SHIFT = 8
CC_AUTO_PAIR = 1 << 16
#: A packed CC byte meaning none.
CC_NONE = 0xFF
#: A CC-map setting is ``CC_MAP_ASSIGNED | msb``, or ``CC_MAP_UNASSIGNED``, as written by 1.0.1.
CC_MAP_ASSIGNED = CC_AUTO_PAIR | CC_NONE << CC_LSB_SHIFT
CC_MAP_UNASSIGNED = CC_MAP_ASSIGNED | CC_NONE
#: Settings key holding front panel SETTINGS > Vid Route Mode.
ROUTE_SETTING_KEY = SettingKey.VIDEO_ROUTE_MODE
#: The route a rig fed only over HDMI runs under.
ROUTE_HDMI_IN = 1


def modulator_key(channel, field):
    """Settings key of one saved modulator field, ``field`` a name of :data:`MODULATOR_FIELDS`."""
    return MODULATOR_SETTING_KEY + int(channel) * len(MODULATOR_FIELDS) + MODULATOR_FIELDS.index(field)


def setting_name(key):
    """Name of a settings key: its panel name, ``P3 time``, ``MIDI Assign P5``, or ``key N``."""
    key = int(key)
    modulator, field = divmod(key - MODULATOR_SETTING_KEY, len(MODULATOR_FIELDS))
    if key in SETTING_NAMES:
        return SETTING_NAMES[key]
    if 0 <= modulator < PARAM_COUNT:
        return f"P{modulator + 1} {MODULATOR_FIELDS[field]}"
    if 0 <= key - CC_MAP_SETTING_KEY < PARAM_COUNT:
        return f"MIDI Assign P{key - CC_MAP_SETTING_KEY + 1}"
    return f"key {key}"


def describe_midi_assign(value):
    """A packed MIDI Assign value as ``CC n``, ``LSB m`` and ``auto``, or ``none``."""
    msb, lsb = value & CC_MSB_MASK, value >> CC_LSB_SHIFT & CC_MSB_MASK
    if msb == CC_NONE:
        return "none"
    parts = [f"CC {msb}"] + ([] if lsb == CC_NONE else [f"LSB {lsb}"])
    return " ".join(parts + (["auto"] if value & CC_AUTO_PAIR else []))


def describe_setting(key, value):
    """``value`` with the guide's name for it, e.g. ``1 (HDMI In)``; other values are shown bare."""
    key = int(key)
    name = SETTING_VALUES.get(key, {}).get(value) if isinstance(value, int) else None
    if name is None and isinstance(value, int) and 0 <= key - CC_MAP_SETTING_KEY < PARAM_COUNT:
        name = describe_midi_assign(value)
    return f"{value!r}" if name is None else f"{value} ({name})"


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

#: Manual reference that makes a MIDI CC absolute: every slot at zero.
PARK_REFERENCE = (PARAM_MIN,) * PARAM_COUNT

#: Default sweep resolution for a parameter with no declared levels.
SWEEP_STEPS = 32


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
    CRITICAL = "critical"
    NONE = "none"


#: An error code is ``domain << ERROR_DOMAIN_SHIFT | item``.
ERROR_DOMAIN_SHIFT = 26
ERROR_ITEM_MASK = 0x3FFF


class ErrorDomain(IntEnum):
    """Firmware domain raising an error: the top 6 bits of its code."""

    SHELL = 22
    PRESETS = 23


class ShellErrorCode(IntEnum):
    """Shell (domain 22) error codes returned as ``!<code>:<message>``."""

    UNKNOWN_COMMAND = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 1
    PARSE_ERROR = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 2
    SERVICE_UNAVAILABLE = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 3
    BUFFER_OVERFLOW = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 4
    NOT_CONNECTED = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 5
    FILE_NOT_FOUND = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 6
    IO_ERROR = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 7
    SD_NOT_MOUNTED = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 8
    PATH_TOO_LONG = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 9
    INVALID_PATH = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 10
    INVALID_STATE = ErrorDomain.SHELL << ERROR_DOMAIN_SHIFT | 11


class PresetErrorCode(IntEnum):
    """Preset (domain 23) error codes."""

    NO_PROGRAM = ErrorDomain.PRESETS << ERROR_DOMAIN_SHIFT | 1
    INVALID_INDEX = ErrorDomain.PRESETS << ERROR_DOMAIN_SHIFT | 2
    NOT_FOUND = ErrorDomain.PRESETS << ERROR_DOMAIN_SHIFT | 3
    NAME_INVALID = ErrorDomain.PRESETS << ERROR_DOMAIN_SHIFT | 4
    SAVE_FAILED = ErrorDomain.PRESETS << ERROR_DOMAIN_SHIFT | 5
    PRESETS_FULL = ErrorDomain.PRESETS << ERROR_DOMAIN_SHIFT | 6
    PARSE_FAILED = ErrorDomain.PRESETS << ERROR_DOMAIN_SHIFT | 7


def decode_error_code(code):
    """``(domain, item)`` of an error code, or None for None."""
    return None if code is None else (code >> ERROR_DOMAIN_SHIFT, code & ERROR_ITEM_MASK)


#: Codes by which the firmware positively lacks a command: unknown, or a usage error for its words.
ABSENT_COMMAND_CODES = frozenset({ShellErrorCode.UNKNOWN_COMMAND, ShellErrorCode.PARSE_ERROR})
#: First words of the shell's command groups. A reply is tagged ``@<first word>:``, so one tagged
#: with another group's word answers some other client's command.
COMMAND_GROUPS = frozenset(
    {
        "cpu",
        "fpga",
        "fs",
        "help",
        "language",
        "log",
        "midi",
        "modulation",
        "msd",
        "program",
        "programs",
        "ram",
        "reboot",
        "remote",
        "screen",
        "serial",
        "settings",
        "status",
        "transport",
        "version",
        "video",
    }
)


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
