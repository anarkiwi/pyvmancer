"""Protocol constants: parameter kinds, operator metadata and name resolution."""

import pytest

from pyvmancer import const
from pyvmancer.const import (
    OPERATORS,
    PARAM_COUNT,
    PER_LINE_PARAMS,
    Operator,
    OperatorCategory,
    ParamKind,
    Transport,
    param_kind,
    resolve_operator,
)


@pytest.mark.parametrize(
    "param,kind",
    [(p, ParamKind.KNOB) for p in range(1, 7)]
    + [(p, ParamKind.TOGGLE) for p in range(7, 12)]
    + [(12, ParamKind.FADER)],
)
def test_param_kind(param, kind):
    """Slots 1-6 are knobs, 7-11 toggles and 12 the fader."""
    assert param_kind(param) is kind


@pytest.mark.parametrize("param", [-1, 0, PARAM_COUNT + 1, 100])
def test_param_kind_out_of_range(param):
    """Parameter numbers are 1-based and bounded."""
    with pytest.raises(ValueError, match="parameter must be"):
        param_kind(param)


@pytest.mark.parametrize(
    "value,expected",
    [
        (Operator.FREE_LFO, Operator.FREE_LFO),
        (1, Operator.FREE_LFO),
        (0, Operator.DISABLED),
        (30, Operator.CV_INPUT),
        ("free lfo", Operator.FREE_LFO),
        ("free_lfo", Operator.FREE_LFO),
        ("FREE_LFO", Operator.FREE_LFO),
        ("Free LFO", Operator.FREE_LFO),
        ("free-lfo", Operator.FREE_LFO),
        ("cv input", Operator.CV_INPUT),
        ("  Bouncing Ball  ", Operator.BOUNCING_BALL),
        ("midi turing", Operator.MIDI_TURING),
    ],
)
def test_resolve_operator(value, expected):
    """Operators resolve from enums, ids and either name spelling."""
    assert resolve_operator(value) is expected


@pytest.mark.parametrize("value", ["nonsense", "", 23, 999, None, 3.5])
def test_resolve_operator_rejects_garbage(value):
    """Undocumented ids and unknown names are refused."""
    with pytest.raises(ValueError):
        resolve_operator(value)


def test_operators_table_is_self_consistent():
    """Every OPERATORS key is the operator its metadata declares."""
    assert set(OPERATORS) == set(Operator)
    for key, info in OPERATORS.items():
        assert key is info.operator
        assert isinstance(info.category, OperatorCategory)
        assert isinstance(info.transport, Transport)
        assert isinstance(info.per_line, bool)
        assert len(info.glyph) == 1


def test_operator_glyphs_are_unique():
    """Glyphs identify an operator on the panel, so they must not collide."""
    glyphs = [info.glyph for info in OPERATORS.values()]
    assert len(set(glyphs)) == len(glyphs)


@pytest.mark.parametrize(
    "operator,name",
    [
        (Operator.FREE_LFO, "Free LFO"),
        (Operator.SYNC_LFO, "Sync LFO"),
        (Operator.CV_INPUT, "CV Input"),
        (Operator.MIDI_TURING, "MIDI Turing"),
        (Operator.FIELD_ACCUM, "Field Accum"),
        (Operator.PROB_GATE, "Prob Gate"),
        (Operator.TURING_MACHINE, "Turing Machine"),
        (Operator.DISABLED, "Disabled"),
    ],
)
def test_operator_info_name(operator, name):
    """Names title-case each word but preserve known acronyms."""
    assert OPERATORS[operator].name == name


def test_operator_info_repr():
    """The repr carries the display name and numeric id."""
    assert repr(OPERATORS[Operator.FREE_LFO]) == "OperatorInfo('Free LFO', id=1)"


def test_per_line_params_are_valid_slots():
    """Per-line rendering only applies to real parameter slots."""
    assert PER_LINE_PARAMS <= set(range(1, PARAM_COUNT + 1))


def test_note_triggered_operators_are_known():
    """Note-triggered operators are members of the operator table."""
    assert const.NOTE_TRIGGERED <= set(OPERATORS)


def test_param_defaults_shape():
    """There is exactly one default per parameter, all in range."""
    assert len(const.PARAM_DEFAULTS) == PARAM_COUNT
    assert all(const.PARAM_MIN <= v <= const.PARAM_MAX for v in const.PARAM_DEFAULTS)
    assert len(const.DEFAULT_CC_MSB) == PARAM_COUNT


def test_embedded_programs_are_sorted_and_unique():
    """The embedded program list is a canonical sorted set."""
    assert list(const.EMBEDDED_PROGRAMS) == sorted(set(const.EMBEDDED_PROGRAMS))
