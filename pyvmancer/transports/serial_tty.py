"""Shell transport over a CDC-ACM tty (``/dev/ttyACM*``)."""

import os
import select
from contextlib import suppress

from ..discovery import attached, find_devices
from ..errors import DeviceGoneError, TransportError, TransportUnavailableError
from ..liveness import POLL_S
from .base import ByteTransport


class SerialTransport(ByteTransport):
    """Byte stream over a :mod:`pyserial` port.

    The Videomancer ignores the baud rate for USB CDC; it is accepted only
    because pyserial requires one.
    """

    def __init__(self, port, baudrate=115200):
        try:
            import serial
        except ImportError as err:
            raise TransportUnavailableError("pyserial is not installed") from err
        self._port_name = port
        tty = os.path.realpath(port)
        self._where = next(((d.bus, d.address) for d in find_devices() if d.tty == tty), None)
        try:
            self._port = serial.Serial(port, baudrate=baudrate, timeout=0)
        except Exception as err:
            raise TransportUnavailableError(f"cannot open {port}: {err}") from err

    @property
    def name(self):
        """Name of the underlying serial port."""
        return self._port_name

    def present(self):
        """Whether the device that exposed this tty when it opened is still enumerated, from sysfs.

        None when the tty could not be traced to a Videomancer at open, since
        presence then cannot be read.
        """
        return None if self._where is None else attached(*self._where)

    def write(self, data):
        """Write every byte, waiting on the port for as long as the device stays enumerated.

        The device stops draining the link while it commits a bulk transfer to
        the card, and abandoning a write mid-payload strands it consuming the
        link as file content, so a write that cannot progress is waited on
        rather than timed out; only the device leaving USB ends it.
        """
        if self._port is None:
            raise TransportError("transport is closed")
        view = memoryview(bytes(data))
        try:
            fd = self._port.fileno()
            while view:
                if select.select([], [fd], [], POLL_S)[1]:
                    with suppress(BlockingIOError):
                        view = view[os.write(fd, view) :]
                elif self.present() is False:
                    raise DeviceGoneError(f"writing to {self._port_name}", f"{len(view)} bytes unsent")
        except DeviceGoneError:
            raise
        except Exception as err:
            raise TransportError(f"write to {self._port_name} failed: {err}") from err

    def read(self, timeout=0.0):
        """Read available bytes, blocking up to ``timeout`` for the first one."""
        if self._port is None:
            raise TransportError("transport is closed")
        try:
            self._port.timeout = max(0.0, timeout)
            first = self._port.read(1)
            if not first:
                return b""
            return first + self._port.read(self._port.in_waiting or 0)
        except Exception as err:
            raise TransportError(f"read from {self._port_name} failed: {err}") from err

    def close(self):
        """Close the serial port."""
        if self._port is not None:
            self._port.close()
            self._port = None
