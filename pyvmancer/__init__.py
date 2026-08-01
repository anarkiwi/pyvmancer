"""Automate the LZX Industries Videomancer over USB MIDI and USB serial."""

from .const import (
    CROSSFADER_PARAM,
    EMBEDDED_PROGRAMS,
    OPERATORS,
    PARAM_COUNT,
    PARAM_DEFAULTS,
    PARAM_MAX,
    PARAM_MIN,
    PARK_REFERENCE,
    SWEEP_STEPS,
    LogLevel,
    Operator,
    OperatorCategory,
    ParamKind,
    ParamRole,
    PresetBank,
    TransportState,
    classify_param,
    param_kind,
    resolve_operator,
)
from .device import ProgramParameter, Videomancer, open_midi, open_shell
from .discovery import DeviceInfo, find_device, find_devices
from .errors import (
    DeviceNotFoundError,
    ShellError,
    ShellTimeoutError,
    TransportError,
    TransportUnavailableError,
    VmancerError,
)
from .midi import MidiController
from .programs import MANIFEST_PATH, ProgramEntry, ProgramManifest
from .shell import Reply, ShellClient, encode_preset, parse_line
from .video import VideoStatus

__version__ = "0.2.0"

__all__ = [
    "CROSSFADER_PARAM",
    "DeviceInfo",
    "DeviceNotFoundError",
    "EMBEDDED_PROGRAMS",
    "LogLevel",
    "MANIFEST_PATH",
    "MidiController",
    "OPERATORS",
    "Operator",
    "OperatorCategory",
    "PARAM_COUNT",
    "PARAM_DEFAULTS",
    "PARAM_MAX",
    "PARAM_MIN",
    "PARK_REFERENCE",
    "ParamKind",
    "ParamRole",
    "ProgramEntry",
    "ProgramManifest",
    "ProgramParameter",
    "PresetBank",
    "Reply",
    "SWEEP_STEPS",
    "ShellClient",
    "ShellError",
    "ShellTimeoutError",
    "TransportError",
    "TransportState",
    "TransportUnavailableError",
    "VideoStatus",
    "Videomancer",
    "VmancerError",
    "__version__",
    "classify_param",
    "encode_preset",
    "find_device",
    "find_devices",
    "open_midi",
    "open_shell",
    "param_kind",
    "parse_line",
    "resolve_operator",
]
