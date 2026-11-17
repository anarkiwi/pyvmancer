"""Read the parameter declarations a ``.vmprog`` package carries in its config section.

Layout per the Videomancer SDK's ``vmprog_format.hpp`` (v1.0): a 64-byte header (magic ``VMPG``),
64-byte TOC entries each holding the BLAKE2b-256 digest of its stored payload, and a 7936-byte
program config (TOC type 1) with the program's twelve parameter configs and factory presets.
"""

import hashlib
import zlib
from enum import Enum, IntEnum
from typing import NamedTuple

import numpy as np

from .const import PARAM_COUNT
from .errors import VmancerError

MAGIC = b"VMPG"
TOC_CONFIG = 1
TOC_COMPRESSED_DEFLATE = 0x1
CONFIG_SIZE = 7936
PARAMS_OFFSET = 730
PRESETS_OFFSET = 7594
MAX_PRESETS = 8

HEADER = np.dtype(
    [
        ("magic", "S4"),
        ("version_major", "<u2"),
        ("version_minor", "<u2"),
        ("header_size", "<u2"),
        ("reserved_pad", "<u2"),
        ("file_size", "<u4"),
        ("flags", "<u4"),
        ("toc_offset", "<u4"),
        ("toc_bytes", "<u4"),
        ("toc_count", "<u4"),
        ("sha256_package", "V32"),
    ]
)
TOC_ENTRY = np.dtype(
    [
        ("type", "<u4"),
        ("flags", "<u4"),
        ("offset", "<u4"),
        ("size", "<u4"),
        ("digest", "V32"),
        ("uncompressed_size", "<u4"),
        ("reserved", "<u4", 3),
    ]
)
PARAM_CONFIG = np.dtype(
    [
        ("parameter_id", "<u4"),
        ("control_mode", "<u4"),
        ("min_value", "<u2"),
        ("max_value", "<u2"),
        ("initial_value", "<u2"),
        ("display_min_value", "<i2"),
        ("display_max_value", "<i2"),
        ("display_float_digits", "u1"),
        ("value_label_count", "u1"),
        ("reserved_pad", "u1", 2),
        ("name_label", "S32"),
        ("value_labels", "S32", 16),
        ("suffix_label", "S4"),
        ("reserved", "u1", 2),
    ]
)
PRESET_CONFIG = np.dtype([("name", "S16"), ("values", "<u2", PARAM_COUNT)])
PROGRAM_FIELDS = np.dtype(
    {
        "names": [
            "program_id",
            "version",
            "core_id",
            "program_name",
            "author",
            "preset_count",
            "supported_timings",
            "program_type",
            "processing_delay_clks",
        ],
        "formats": ["S64", ("<u2", 3), "<u4", "S32", "S64", "u1", "<u2", "u1", "<u2"],
        "offsets": [0, 64, 82, 86, 118, 728, 7914, 7916, 7917],
        "itemsize": CONFIG_SIZE,
    }
)


class ControlMode(IntEnum):
    """``vmprog_parameter_control_mode_v1_0``."""

    LINEAR = 0
    LINEAR_HALF = 1
    LINEAR_QUARTER = 2
    LINEAR_DOUBLE = 3
    BOOLEAN = 4
    STEPS_4 = 5
    STEPS_8 = 6
    STEPS_16 = 7
    STEPS_32 = 8
    STEPS_64 = 9
    STEPS_128 = 10
    STEPS_256 = 11
    POLAR_DEGS_90 = 12
    POLAR_DEGS_180 = 13
    POLAR_DEGS_360 = 14
    POLAR_DEGS_720 = 15
    POLAR_DEGS_1440 = 16
    POLAR_DEGS_2880 = 17
    QUAD_IN = 18
    QUAD_OUT = 19
    QUAD_IN_OUT = 20
    SINE_IN = 21
    SINE_OUT = 22
    SINE_IN_OUT = 23
    CIRC_IN = 24
    CIRC_OUT = 25
    CIRC_IN_OUT = 26
    QUINT_IN = 27
    QUINT_OUT = 28
    QUINT_IN_OUT = 29
    QUART_IN = 30
    QUART_OUT = 31
    QUART_IN_OUT = 32
    EXPO_IN = 33
    EXPO_OUT = 34
    EXPO_IN_OUT = 35


class ParamType(Enum):
    """What a program declares one parameter slot to be; ``UNDECLARED`` when no declaration is known."""

    UNUSED = "unused"
    BOOLEAN = "boolean"
    QUANTIZED = "quantized"
    CONTINUOUS = "continuous"
    UNDECLARED = "undeclared"


def _text(raw):
    """A NUL-terminated UTF-8 field."""
    return bytes(raw).split(b"\0", 1)[0].decode("utf-8", "replace")


def _mode(value):
    """``value`` as a :class:`ControlMode` where it is one, else the raw integer."""
    try:
        return ControlMode(int(value))
    except ValueError:
        return int(value)


class ParamDecl(NamedTuple):
    """One declared parameter config; ``parameter_id`` is its 1-based slot."""

    parameter_id: int
    control_mode: object
    minimum: int
    maximum: int
    initial: int
    display_min: int
    display_max: int
    float_digits: int
    name: str
    value_labels: tuple
    suffix: str

    @property
    def levels(self):
        """Distinct positions the declaration gives the slot, or ``None`` for a continuous one."""
        labels = len(self.value_labels)
        if self.control_mode == ControlMode.BOOLEAN or labels == 2:
            return 2
        if labels > 2:
            return labels
        if ControlMode.STEPS_4 <= self.control_mode <= ControlMode.STEPS_256:
            return 4 << (self.control_mode - ControlMode.STEPS_4)
        return None

    @property
    def type(self):
        """:class:`ParamType` the declaration gives the slot."""
        levels = self.levels
        if levels is None:
            return ParamType.CONTINUOUS
        return ParamType.BOOLEAN if levels == 2 else ParamType.QUANTIZED

    @classmethod
    def from_record(cls, record):
        """Build from one :data:`PARAM_CONFIG` record."""
        count = min(int(record["value_label_count"]), len(record["value_labels"]))
        return cls(
            int(record["parameter_id"]),
            _mode(record["control_mode"]),
            int(record["min_value"]),
            int(record["max_value"]),
            int(record["initial_value"]),
            int(record["display_min_value"]),
            int(record["display_max_value"]),
            int(record["display_float_digits"]),
            _text(record["name_label"]),
            tuple(_text(label) for label in record["value_labels"][:count]),
            _text(record["suffix_label"]),
        )


class Preset(NamedTuple):
    """A factory preset: its name and twelve raw manual values, P1 first."""

    name: str
    values: tuple


class VmprogConfig(NamedTuple):
    """A program's declared identity, parameters and factory presets."""

    program_id: str
    version: tuple
    core_id: int
    name: str
    author: str
    declarations: tuple
    presets: tuple
    supported_timings: int
    program_type: int
    processing_delay_clks: int

    def declaration(self, param):
        """The :class:`ParamDecl` for 1-based slot ``param``, or ``None`` where it is not declared."""
        if not 1 <= param <= PARAM_COUNT:
            raise ValueError(f"parameter must be 1..{PARAM_COUNT}, got {param}")
        return next((d for d in self.declarations if d.parameter_id == param), None)

    def param_type(self, param):
        """:class:`ParamType` of 1-based slot ``param``; a slot with no declaration is unused."""
        decl = self.declaration(param)
        return ParamType.UNUSED if decl is None else decl.type


def _records(data, dtype, offset, count, what):
    """``count`` ``dtype`` records at ``offset`` of ``data``, raising if they overrun it."""
    if offset < 0 or offset + dtype.itemsize * count > len(data):
        raise VmancerError(f"vmprog {what} at {offset} overruns {len(data)} bytes")
    return np.frombuffer(data, dtype=dtype, count=count, offset=offset)


def config_section(data):
    """The digest-verified, decompressed program config bytes of a ``.vmprog`` package."""
    data = bytes(data)
    header = _records(data, HEADER, 0, 1, "header")[0]
    if header["magic"] != MAGIC:
        raise VmancerError(f"not a vmprog package: magic {bytes(header['magic'])!r}")
    toc = _records(data, TOC_ENTRY, int(header["toc_offset"]), int(header["toc_count"]), "toc")
    for entry in toc[toc["type"] == TOC_CONFIG]:
        start, size = int(entry["offset"]), int(entry["size"])
        if start + size > len(data):
            raise VmancerError(f"vmprog config at {start}+{size} overruns {len(data)} bytes")
        stored = data[start : start + size]
        if hashlib.blake2b(stored, digest_size=32).digest() != bytes(entry["digest"]):
            raise VmancerError("vmprog config fails its BLAKE2b-256 digest")
        config = zlib.decompress(stored, -15) if entry["flags"] & TOC_COMPRESSED_DEFLATE else stored
        if len(config) != CONFIG_SIZE:
            raise VmancerError(f"vmprog config is {len(config)} bytes, want {CONFIG_SIZE}")
        return config
    raise VmancerError("vmprog package has no config section")


def parse_config(config):
    """A :class:`VmprogConfig` from the 7936 config bytes."""
    head = _records(config, PROGRAM_FIELDS, 0, 1, "config")[0]
    params = _records(config, PARAM_CONFIG, PARAMS_OFFSET, PARAM_COUNT, "parameters")
    presets = _records(config, PRESET_CONFIG, PRESETS_OFFSET, MAX_PRESETS, "presets")
    declared = params[(params["parameter_id"] >= 1) & (params["parameter_id"] <= PARAM_COUNT)]
    return VmprogConfig(
        program_id=_text(head["program_id"]),
        version=tuple(int(v) for v in head["version"]),
        core_id=int(head["core_id"]),
        name=_text(head["program_name"]),
        author=_text(head["author"]),
        declarations=tuple(ParamDecl.from_record(r) for r in declared),
        presets=tuple(
            Preset(_text(p["name"]), tuple(int(v) for v in p["values"]))
            for p in presets[: min(int(head["preset_count"]), MAX_PRESETS)]
        ),
        supported_timings=int(head["supported_timings"]),
        program_type=int(head["program_type"]),
        processing_delay_clks=int(head["processing_delay_clks"]),
    )


def read_vmprog(source):
    """The :class:`VmprogConfig` of a ``.vmprog`` package, from a path or its bytes."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return parse_config(config_section(source))
    with open(source, "rb") as handle:
        return parse_config(config_section(handle.read()))
