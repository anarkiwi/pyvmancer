"""Unit conversions between device units, normalised floats and MIDI words."""

import numpy as np
import pytest

from pyvmancer import scaling
from pyvmancer.const import BOOL_THRESHOLD, PARAM_MAX, PARAM_MIN

ALL_DEVICE = np.arange(PARAM_MIN, PARAM_MAX + 1, dtype=np.int64)


def test_midi14_round_trips_exactly():
    """14-bit words carry more resolution than device units, so the trip is lossless."""
    assert np.array_equal(scaling.from_midi14(scaling.to_midi14(ALL_DEVICE)), ALL_DEVICE)


def test_midi7_round_trip_error_is_bounded():
    """7-bit CC values are lossy but never off by more than half a 7-bit step."""
    error = np.abs(scaling.from_midi7(scaling.to_midi7(ALL_DEVICE)) - ALL_DEVICE)
    assert error.max() <= PARAM_MAX // (2 * scaling.MIDI_7BIT_MAX) + 1


def test_midi7_endpoints():
    """The extremes of the device range map onto the extremes of the CC range."""
    assert int(scaling.to_midi7(PARAM_MIN)) == 0
    assert int(scaling.to_midi7(PARAM_MAX)) == scaling.MIDI_7BIT_MAX
    assert int(scaling.from_midi7(0)) == PARAM_MIN
    assert int(scaling.from_midi7(scaling.MIDI_7BIT_MAX)) == PARAM_MAX


def test_midi14_endpoints():
    """Likewise for 14-bit words."""
    assert int(scaling.to_midi14(PARAM_MAX)) == scaling.MIDI_14BIT_MAX
    assert int(scaling.from_midi14(scaling.MIDI_14BIT_MAX)) == PARAM_MAX


def test_conversions_clip_out_of_range_inputs():
    """Inputs beyond either end of a range are clipped, not wrapped."""
    assert int(scaling.to_midi7(5000)) == scaling.MIDI_7BIT_MAX
    assert int(scaling.to_midi14(-5)) == 0
    assert int(scaling.from_midi7(999)) == PARAM_MAX
    assert int(scaling.from_midi14(-1)) == PARAM_MIN


def test_split_join14_round_trips():
    """Splitting and rejoining 14-bit words is an identity."""
    words = np.arange(0, scaling.MIDI_14BIT_MAX + 1, dtype=np.int64)
    msb, lsb = scaling.split14(words)
    assert msb.max() <= scaling.MIDI_7BIT_MAX
    assert lsb.max() <= scaling.MIDI_7BIT_MAX
    assert np.array_equal(scaling.join14(msb, lsb), words)


def test_split14_clips_and_join14_clips():
    """Out-of-range words and 7-bit halves are clipped before packing."""
    msb, lsb = scaling.split14(99999)
    assert (int(msb), int(lsb)) == (scaling.MIDI_14BIT_MAX >> 7, scaling.MIDI_14BIT_MAX & 0x7F)
    assert int(scaling.join14(999, 999)) == scaling.MIDI_14BIT_MAX


def test_clamp_and_unit_conversions():
    """clamp/to_device/to_unit agree at the range endpoints."""
    assert np.array_equal(scaling.clamp([-10, 500, 9999]), np.array([0, 500, PARAM_MAX]))
    assert int(scaling.to_device(1.0)) == PARAM_MAX
    assert int(scaling.to_device(0.0)) == PARAM_MIN
    assert int(scaling.to_device(2.0)) == PARAM_MAX
    assert int(scaling.to_device(-1.0)) == PARAM_MIN
    assert scaling.to_unit(PARAM_MAX) == pytest.approx(1.0)
    assert scaling.to_unit(PARAM_MIN) == pytest.approx(0.0)


def test_to_device_to_unit_round_trip():
    """Normalising and de-normalising every device unit is lossless."""
    assert np.array_equal(scaling.to_device(scaling.to_unit(ALL_DEVICE)), ALL_DEVICE)


@pytest.mark.parametrize(
    "value,expected", [(0, False), (BOOL_THRESHOLD - 1, False), (BOOL_THRESHOLD, True), (PARAM_MAX, True)]
)
def test_as_bool_threshold(value, expected):
    """The toggle midpoint is inclusive on the "on" side."""
    assert bool(scaling.as_bool(value)) is expected


def test_as_bool_and_from_bool_vectorise():
    """Both toggle helpers accept arrays."""
    values = np.array([0, 511, 512, 1023])
    assert np.array_equal(scaling.as_bool(values), np.array([False, False, True, True]))
    assert np.array_equal(scaling.from_bool([True, False]), np.array([PARAM_MAX, PARAM_MIN]))


@pytest.mark.parametrize(
    "value,boolean,expected",
    [
        (True, False, PARAM_MAX),
        (False, False, PARAM_MIN),
        (np.bool_(True), False, PARAM_MAX),
        (0.0, False, PARAM_MIN),
        (0.5, False, 512),
        (0.75, False, 767),
        (1.0, False, PARAM_MAX),
        (np.float64(0.25), False, 256),
        (400, False, 400),
        (-5, False, PARAM_MIN),
        (5000, False, PARAM_MAX),
        (0, True, PARAM_MIN),
        (1, True, PARAM_MAX),
        (0.4, True, PARAM_MAX),
        (0.0, True, PARAM_MIN),
    ],
)
def test_coerce_value(value, boolean, expected):
    """Coercion dispatches on type, and ``boolean`` forces truthiness."""
    result = scaling.coerce_value(value, boolean=boolean)
    assert isinstance(result, int)
    assert result == expected
