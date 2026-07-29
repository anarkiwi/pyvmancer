"""MIDI control surface: parameters, note triggers, program change and clock.

Message encoding is delegated to :mod:`mido`, which is pure Python and needs no
port backend; the bytes are handed to whichever
:class:`~pyvmancer.transports.base.MidiTransport` is in use.
"""

import time

import mido
import numpy as np

from . import scaling
from .const import (
    CC_LSB_OFFSET,
    CC_MAX,
    CC_MIN,
    DEFAULT_CC_MSB,
    MIDI_CLOCK_PPQN,
    NOTE_BASE,
    PARAM_COUNT,
    PARAM_DEFAULTS,
    ParamKind,
    param_kind,
)
from .errors import VmancerError


def _check_param(param):
    """Validate and return a 1-based parameter number."""
    if not 1 <= param <= PARAM_COUNT:
        raise ValueError(f"parameter must be 1..{PARAM_COUNT}, got {param}")
    return param


class MidiController:
    """Drives a Videomancer over MIDI.

    ``channel`` is 1-16, or ``None`` to target channel 1 while the device is in
    its default Omni mode. ``high_resolution`` sends 14-bit MSB/LSB CC pairs.
    """

    def __init__(self, transport, channel=None, high_resolution=True, cc_map=None):
        self._transport = transport
        self.channel = channel
        self.high_resolution = high_resolution
        self._cc_map = np.array(DEFAULT_CC_MSB if cc_map is None else cc_map, dtype=np.int64)
        if self._cc_map.shape != (PARAM_COUNT,):
            raise ValueError(f"cc_map must have {PARAM_COUNT} entries")
        if np.any((self._cc_map < CC_MIN) | (self._cc_map > CC_MAX)):
            raise ValueError(f"cc_map entries must be {CC_MIN}..{CC_MAX}")
        self._state = np.array(PARAM_DEFAULTS, dtype=np.int64)

    @property
    def transport(self):
        """The underlying MIDI transport."""
        return self._transport

    @property
    def _channel_index(self):
        """Zero-based MIDI channel used for channel-voice messages."""
        if self.channel is None:
            return 0
        if not 1 <= self.channel <= 16:
            raise ValueError(f"channel must be 1..16 or None, got {self.channel}")
        return self.channel - 1

    @property
    def cc_map(self):
        """Copy of the per-parameter CC (MSB) assignment."""
        return self._cc_map.copy()

    def assign_cc(self, param, cc):
        """Record that ``param`` is assigned to ``cc`` on the device.

        This mirrors an assignment already made on the hardware; the device has
        no remote-assign message, so it is set in the device's MIDI Assign mode.
        """
        _check_param(param)
        if not CC_MIN <= cc <= CC_MAX:
            raise ValueError(f"cc must be {CC_MIN}..{CC_MAX}, got {cc}")
        collision = np.flatnonzero(self._cc_map == cc)
        if collision.size and collision[0] != param - 1:
            raise VmancerError(f"cc {cc} is already assigned to parameter {collision[0] + 1}")
        self._cc_map[param - 1] = cc
        return self

    @property
    def state(self):
        """Copy of the last parameter values sent, in device units."""
        return self._state.copy()

    def _send(self, *messages):
        """Serialise messages and hand the bytes to the transport."""
        payload = bytearray()
        for message in messages:
            payload.extend(message.bytes())
        self._transport.send(bytes(payload))

    def _cc_messages(self, param, value):
        """Build the CC message(s) carrying ``value`` for ``param``."""
        channel = self._channel_index
        msb_cc = int(self._cc_map[param - 1])
        if not self.high_resolution:
            return [
                mido.Message(
                    "control_change", channel=channel, control=msb_cc, value=int(scaling.to_midi7(value))
                )
            ]
        msb, lsb = scaling.split14(scaling.to_midi14(value))
        lsb_cc = msb_cc + CC_LSB_OFFSET
        messages = [mido.Message("control_change", channel=channel, control=msb_cc, value=int(msb))]
        if lsb_cc <= CC_MAX:
            messages.append(mido.Message("control_change", channel=channel, control=lsb_cc, value=int(lsb)))
        return messages

    def set_param(self, param, value):
        """Set one parameter.

        ``value`` may be a ``bool`` (toggle), a ``float`` in ``0.0..1.0``, or an
        ``int`` in device units ``0..1023``.
        """
        _check_param(param)
        boolean = param_kind(param) is ParamKind.TOGGLE
        device_value = scaling.coerce_value(value, boolean=boolean)
        self._state[param - 1] = device_value
        self._send(*self._cc_messages(param, device_value))
        return self

    def set_params(self, values):
        """Set several parameters from a mapping or a 12-element sequence."""
        items = values.items() if hasattr(values, "items") else enumerate(values, start=1)
        for param, value in items:
            self.set_param(param, value)
        return self

    def get_param(self, param):
        """Return the last value sent for ``param``, in device units."""
        return int(self._state[_check_param(param) - 1])

    def reset_params(self):
        """Send every parameter back to its program default."""
        return self.set_params(PARAM_DEFAULTS)

    def knob(self, index, value):
        """Set knob 1-6 (parameters P1-P6)."""
        if not 1 <= index <= 6:
            raise ValueError(f"knob index must be 1..6, got {index}")
        return self.set_param(index, value)

    def toggle(self, index, value):
        """Set toggle 1-5 (parameters P7-P11)."""
        if not 1 <= index <= 5:
            raise ValueError(f"toggle index must be 1..5, got {index}")
        return self.set_param(index + 6, bool(value))

    def fader(self, value):
        """Set the fader (parameter P12)."""
        return self.set_param(PARAM_COUNT, value)

    def select_preset(self, program):
        """Recall a preset with a MIDI Program Change (0-127)."""
        if not 0 <= program <= 127:
            raise ValueError(f"program must be 0..127, got {program}")
        self._send(mido.Message("program_change", channel=self._channel_index, program=program))
        return self

    def trigger(self, param, velocity=127):
        """Send note-on for ``param`` to fire its note-triggered operator."""
        _check_param(param)
        self._send(
            mido.Message(
                "note_on", channel=self._channel_index, note=NOTE_BASE + param - 1, velocity=velocity
            )
        )
        return self

    def release(self, param):
        """Send note-off for ``param``."""
        _check_param(param)
        self._send(
            mido.Message("note_off", channel=self._channel_index, note=NOTE_BASE + param - 1, velocity=0)
        )
        return self

    def pulse(self, param, duration=0.05, velocity=127):
        """Trigger ``param`` and release it after ``duration`` seconds."""
        self.trigger(param, velocity=velocity)
        time.sleep(duration)
        return self.release(param)

    def start(self):
        """Send MIDI Start."""
        self._send(mido.Message("start"))
        return self

    def stop(self):
        """Send MIDI Stop, which also resets timecode to zero."""
        self._send(mido.Message("stop"))
        return self

    def cont(self):
        """Send MIDI Continue."""
        self._send(mido.Message("continue"))
        return self

    def clock_tick(self, count=1):
        """Send ``count`` MIDI Clock ticks (24 per quarter note)."""
        self._send(*[mido.Message("clock")] * count)
        return self

    def run_clock(self, bpm, beats, start=True):
        """Emit a tempo-accurate MIDI clock for ``beats`` quarter notes.

        Blocking, and paced against a monotonic clock so drift does not
        accumulate over long runs.
        """
        if bpm <= 0:
            raise ValueError(f"bpm must be positive, got {bpm}")
        if start:
            self.start()
        interval = 60.0 / (bpm * MIDI_CLOCK_PPQN)
        ticks = int(round(beats * MIDI_CLOCK_PPQN))
        origin = time.monotonic()
        for tick in range(ticks):
            delay = origin + tick * interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.clock_tick()
        return self

    def all_notes_off(self):
        """Release every modulator note."""
        for param in range(1, PARAM_COUNT + 1):
            self.release(param)
        return self

    def receive(self, timeout=0.0):
        """Return decoded messages sent by the device (MIDI Out Mode must be on)."""
        data = self._transport.receive(timeout=timeout)
        return list(mido.parse_all(data)) if data else []

    def close(self):
        """Close the underlying transport."""
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
