"""Linux ALSA rawmidi transport.

Talks to ``/dev/snd/midiC<card>D<device>`` with plain file descriptors, so it
needs neither ``libasound`` nor ``python-rtmidi``. This is the only MIDI path
that works in containers without the ALSA userspace libraries installed.
"""

import os
import select

from ..errors import TransportError, TransportUnavailableError
from .base import MidiTransport


class RawMidiTransport(MidiTransport):
    """MIDI over an ALSA rawmidi character device."""

    def __init__(self, path):
        self._path = path
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as err:
            raise TransportUnavailableError(f"cannot open {path}: {err}") from err

    @property
    def name(self):
        """Path of the rawmidi node."""
        return self._path

    def send(self, data):
        """Write raw MIDI bytes, retrying on a full kernel buffer."""
        if self._fd is None:
            raise TransportError("transport is closed")
        view = memoryview(bytes(data))
        while view:
            try:
                written = os.write(self._fd, view)
            except BlockingIOError:
                select.select([], [self._fd], [], 1.0)
                continue
            except OSError as err:
                raise TransportError(f"write to {self._path} failed: {err}") from err
            view = view[written:]

    def receive(self, timeout=0.0):
        """Read whatever MIDI bytes are buffered, waiting up to ``timeout``."""
        if self._fd is None:
            raise TransportError("transport is closed")
        if not select.select([self._fd], [], [], timeout)[0]:
            return b""
        chunks = []
        while True:
            try:
                chunk = os.read(self._fd, 4096)
            except BlockingIOError:
                break
            except OSError as err:
                raise TransportError(f"read from {self._path} failed: {err}") from err
            if not chunk:
                break
            chunks.append(chunk)
            if len(chunk) < 4096:
                break
        return b"".join(chunks)

    def close(self):
        """Close the rawmidi file descriptor."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
