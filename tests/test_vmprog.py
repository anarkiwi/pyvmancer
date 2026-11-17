"""``.vmprog`` declaration reader: layout, digest check and declared parameter types."""

import os

import pytest

from pyvmancer.errors import VmancerError
from pyvmancer.vmprog import ControlMode, ParamType, config_section, read_vmprog

from .vmprog_pack import pack_config, pack_vmprog

QUILT = os.environ.get("PYVMANCER_VMPROG_QUILT", "")

PROGRAM = {
    "program_id": "org.example.patch",
    "program_name": "Patch",
    "author": "example",
    "version": (1, 2, 3),
}
PARAMETERS = [
    {"parameter_id": "rotary_potentiometer_1", "name_label": "Grid", "value_labels": ["A", "B", "C", "D"]},
    {"parameter_id": "rotary_potentiometer_2", "name_label": "Seed", "min_value": 0, "max_value": 1023},
    {"parameter_id": "rotary_potentiometer_3", "name_label": "Shift", "control_mode": "steps_4"},
    {"parameter_id": "rotary_potentiometer_4", "name_label": "Fine", "control_mode": "steps_256"},
    {"parameter_id": "rotary_potentiometer_5", "name_label": "Turn", "control_mode": "polar_degs_90"},
    {"parameter_id": "toggle_switch_7", "name_label": "Seams", "value_labels": ["Off", "On"]},
    {"parameter_id": "toggle_switch_9", "name_label": "Gate", "control_mode": "boolean", "max_value": 1},
    {
        "parameter_id": "linear_potentiometer_12",
        "name_label": "Density",
        "initial_value": 700,
        "display_min_value": 0,
        "display_max_value": 100,
        "suffix_label": "%",
    },
]
PRESETS = [
    {"name": "Dense", "rotary_potentiometer_1": 600, "toggle_switch_7": 1023, "linear_potentiometer_12": 820}
]


@pytest.fixture(name="config")
def config_fixture():
    """The packed test program's parsed config."""
    return read_vmprog(pack_vmprog(pack_config(PROGRAM, PARAMETERS, PRESETS)))


def test_identity_fields(config):
    """Program id, version, name and author come from their documented offsets."""
    assert (config.program_id, config.version, config.name, config.author) == (
        "org.example.patch",
        (1, 2, 3),
        "Patch",
        "example",
    )
    assert config.core_id == 1


@pytest.mark.parametrize(
    "param,kind,levels",
    [
        (1, ParamType.QUANTIZED, 4),
        (2, ParamType.CONTINUOUS, None),
        (3, ParamType.QUANTIZED, 4),
        (4, ParamType.QUANTIZED, 256),
        (5, ParamType.CONTINUOUS, None),
        (6, ParamType.UNUSED, None),
        (7, ParamType.BOOLEAN, 2),
        (8, ParamType.UNUSED, None),
        (9, ParamType.BOOLEAN, 2),
        (12, ParamType.CONTINUOUS, None),
    ],
)
def test_declared_types(config, param, kind, levels):
    """Each slot's type comes from its declaration alone; an undeclared slot is unused."""
    assert config.param_type(param) is kind
    decl = config.declaration(param)
    assert (decl is None) == (kind is ParamType.UNUSED)
    assert decl is None or decl.levels == levels


def test_declaration_fields(config):
    """Labels, ranges and the suffix decode as the converter wrote them."""
    grid, density = config.declaration(1), config.declaration(12)
    assert grid.value_labels == ("A", "B", "C", "D") and (grid.minimum, grid.maximum) == (0, 3)
    assert (
        grid.control_mode is ControlMode.LINEAR and config.declaration(7).control_mode is ControlMode.BOOLEAN
    )
    assert (density.name, density.initial, density.display_max, density.suffix) == ("Density", 700, 100, "%")
    with pytest.raises(ValueError):
        config.declaration(13)


def test_presets(config):
    """Only ``preset_count`` presets are read, values P1 first."""
    assert [p.name for p in config.presets] == ["Dense"]
    assert config.presets[0].values == (600,) + (0,) * 5 + (1023,) + (0,) * 4 + (820,)


def test_compressed_config_matches_stored():
    """A deflated config section reads back the same as a stored one."""
    raw = pack_config(PROGRAM, PARAMETERS, PRESETS)
    assert config_section(pack_vmprog(raw, compress=True)) == raw


def test_unknown_control_mode_stays_an_integer():
    """A control mode outside the SDK enum is kept raw and counts as continuous."""
    raw = bytearray(pack_config(PROGRAM, PARAMETERS[1:2]))
    raw[734] = 99
    decl = read_vmprog(pack_vmprog(bytes(raw))).declaration(2)
    assert decl.control_mode == 99 and decl.type is ParamType.CONTINUOUS


@pytest.mark.parametrize(
    "package,match",
    [
        (b"VMPX" + bytes(60), "not a vmprog"),
        (b"VMPG", "header"),
        (pack_vmprog(pack_config(PROGRAM, []), digest=bytes(32)), "BLAKE2b-256"),
        (pack_vmprog(pack_config(PROGRAM, []), entry_type=4), "no config section"),
        (pack_vmprog(pack_config(PROGRAM, [])[:100]), "100 bytes"),
        (pack_vmprog(pack_config(PROGRAM, []))[:4000], "overruns"),
    ],
)
def test_malformed_packages(package, match):
    """Bad magic, truncation, a digest mismatch or a missing config raise rather than guess."""
    with pytest.raises(VmancerError, match=match):
        read_vmprog(package)


def test_reads_a_path(tmp_path):
    """A path on disk reads the same as its bytes."""
    path = tmp_path / "patch.vmprog"
    path.write_bytes(pack_vmprog(pack_config(PROGRAM, PARAMETERS)))
    assert read_vmprog(str(path)).name == "Patch"


@pytest.mark.skipif(
    not os.path.exists(QUILT), reason="set PYVMANCER_VMPROG_QUILT to a published quilt.vmprog"
)
def test_published_quilt_package():
    """A published package: P12 is Density, a continuous fader, and every slot is declared."""
    config = read_vmprog(QUILT)
    assert config.program_id == "com.czartroniks.quilt" and config.declaration(12).name == "Density"
    assert [config.param_type(p) for p in (1, 2, 7, 12)] == [
        ParamType.QUANTIZED,
        ParamType.CONTINUOUS,
        ParamType.BOOLEAN,
        ParamType.CONTINUOUS,
    ]
