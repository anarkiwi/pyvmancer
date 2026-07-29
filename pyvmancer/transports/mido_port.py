"""Portable MIDI transport backed by :mod:`mido` (macOS, Windows, Linux+ALSA)."""

import time

from ..errors import TransportError, TransportUnavailableError
from .base import MidiTransport


class MidoTransport(MidiTransport):
    """MIDI over a named :mod:`mido` port pair.

    Requires a working :mod:`mido` backend (``python-rtmidi`` needs
    ``libasound`` on Linux); use :class:`~pyvmancer.transports.rawmidi.
    RawMidiTransport` where that is unavailable.
    """

    def __init__(self, output_name, input_name=None):
        try:
            import mido
        except ImportError as err:
            raise TransportUnavailableError("mido is not installed") from err
        self._mido = mido
        self._output_name = output_name
        self._input_name = input_name if input_name is not None else output_name
        try:
            self._out = mido.open_output(output_name)
        except Exception as err:
            raise TransportUnavailableError(f"cannot open MIDI output {output_name!r}: {err}") from err
        try:
            self._in = mido.open_input(self._input_name)
        except Exception:
            self._in = None

    @property
    def name(self):
        """Name of the underlying mido output port."""
        return self._output_name

    def send(self, data):
        """Parse raw bytes into messages and send them."""
        if self._out is None:
            raise TransportError("transport is closed")
        try:
            for message in self._mido.parse_all(bytes(data)):
                self._out.send(message)
        except Exception as err:
            raise TransportError(f"send on {self._output_name!r} failed: {err}") from err

    def receive(self, timeout=0.0):
        """Drain pending input messages back into raw bytes."""
        if self._in is None:
            return b""
        deadline = time.monotonic() + max(0.0, timeout)
        out = bytearray()
        while True:
            message = self._in.poll()
            if message is not None:
                out.extend(message.bytes())
                continue
            if out or time.monotonic() >= deadline:
                return bytes(out)
            time.sleep(0.001)

    def close(self):
        """Close both ports."""
        for port in (self._out, self._in):
            if port is not None:
                port.close()
        self._out = None
        self._in = None
