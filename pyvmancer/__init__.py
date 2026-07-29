"""Automate the LZX Industries Videomancer over USB MIDI and USB serial."""

from .const import (
    EMBEDDED_PROGRAMS,
    OPERATORS,
    PARAM_COUNT,
    PARAM_DEFAULTS,
    PARAM_MAX,
    PARAM_MIN,
    LogLevel,
    Operator,
    OperatorCategory,
    ParamKind,
    PresetBank,
    TransportState,
    param_kind,
    resolve_operator,
)
from .device import Videomancer, open_midi, open_shell
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
from .shell import Reply, ShellClient, encode_preset, parse_line

__version__ = "0.1.0"

__all__ = [
    "DeviceInfo",
    "DeviceNotFoundError",
    "EMBEDDED_PROGRAMS",
    "LogLevel",
    "MidiController",
    "OPERATORS",
    "Operator",
    "OperatorCategory",
    "PARAM_COUNT",
    "PARAM_DEFAULTS",
    "PARAM_MAX",
    "PARAM_MIN",
    "ParamKind",
    "PresetBank",
    "Reply",
    "ShellClient",
    "ShellError",
    "ShellTimeoutError",
    "TransportError",
    "TransportState",
    "TransportUnavailableError",
    "Videomancer",
    "VmancerError",
    "__version__",
    "encode_preset",
    "find_device",
    "find_devices",
    "open_midi",
    "open_shell",
    "param_kind",
    "parse_line",
    "resolve_operator",
]
