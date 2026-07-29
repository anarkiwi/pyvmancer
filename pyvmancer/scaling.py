"""Vectorised conversions between device units, normalised floats and MIDI words.

Device parameters are 10-bit (0..1023). MIDI carries them as 7-bit CC values or
as 14-bit MSB/LSB CC pairs, so every conversion is a pure integer rescale.
"""

import numpy as np

from .const import BOOL_THRESHOLD, PARAM_MAX, PARAM_MIN

MIDI_7BIT_MAX = 127
MIDI_14BIT_MAX = 16383


def _rescale(values, src_max, dst_max):
    """Round-half-up integer rescale of ``values`` from ``src_max`` to ``dst_max``."""
    arr = np.asarray(values, dtype=np.int64)
    scaled = (arr * dst_max + src_max // 2) // src_max
    return np.clip(scaled, 0, dst_max)


def clamp(values):
    """Clip device-unit values into ``PARAM_MIN..PARAM_MAX`` as int64."""
    return np.clip(np.asarray(values, dtype=np.int64), PARAM_MIN, PARAM_MAX)


def to_device(values):
    """Convert normalised floats in ``0.0..1.0`` to device units."""
    arr = np.asarray(values, dtype=np.float64)
    return np.clip(np.rint(arr * PARAM_MAX), PARAM_MIN, PARAM_MAX).astype(np.int64)


def to_unit(values):
    """Convert device units to normalised floats in ``0.0..1.0``."""
    return clamp(values).astype(np.float64) / PARAM_MAX


def to_midi7(values):
    """Convert device units to 7-bit CC values."""
    return _rescale(clamp(values), PARAM_MAX, MIDI_7BIT_MAX)


def from_midi7(values):
    """Convert 7-bit CC values to device units."""
    return _rescale(np.clip(values, 0, MIDI_7BIT_MAX), MIDI_7BIT_MAX, PARAM_MAX)


def to_midi14(values):
    """Convert device units to 14-bit words."""
    return _rescale(clamp(values), PARAM_MAX, MIDI_14BIT_MAX)


def from_midi14(values):
    """Convert 14-bit words to device units."""
    return _rescale(np.clip(values, 0, MIDI_14BIT_MAX), MIDI_14BIT_MAX, PARAM_MAX)


def split14(words):
    """Split 14-bit words into ``(msb, lsb)`` 7-bit arrays."""
    arr = np.clip(np.asarray(words, dtype=np.int64), 0, MIDI_14BIT_MAX)
    return arr >> 7, arr & 0x7F


def join14(msb, lsb):
    """Join 7-bit MSB/LSB arrays into 14-bit words."""
    hi = np.clip(np.asarray(msb, dtype=np.int64), 0, MIDI_7BIT_MAX)
    lo = np.clip(np.asarray(lsb, dtype=np.int64), 0, MIDI_7BIT_MAX)
    return (hi << 7) | lo


def as_bool(values):
    """Interpret device units as toggle states using the documented midpoint."""
    return clamp(values) >= BOOL_THRESHOLD


def from_bool(states):
    """Render toggle states as device units at the extremes of the range."""
    return np.where(np.asarray(states, dtype=bool), PARAM_MAX, PARAM_MIN).astype(np.int64)


def coerce_value(value, boolean=False):
    """Coerce a scalar user value to device units.

    Accepts ``bool`` (toggle), ``float`` in ``0.0..1.0`` (normalised) or ``int``
    (already device units). ``boolean`` forces truthiness interpretation.
    """
    if boolean or isinstance(value, (bool, np.bool_)):
        return int(from_bool(bool(value)))
    if isinstance(value, (float, np.floating)):
        return int(to_device(value))
    return int(clamp(int(value)))
