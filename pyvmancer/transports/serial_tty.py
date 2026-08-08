"""Shell transport over a CDC-ACM tty (``/dev/ttyACM*``, ``COM*``)."""

from ..errors import TransportError, TransportUnavailableError
from .base import ByteTransport

#: Seconds to let a write block before giving up.
#:
#: The device stops draining the link while it commits a bulk transfer to the
#: SD card, and those pauses run past two seconds when ``fs put`` streams a
#: program-sized payload into a directory that already holds the whole library.
#: Abandoning a write mid-payload strands the device consuming the link as file
#: content, so this is generous on purpose; the shell's own reply deadlines are
#: what bound a genuinely unresponsive device.
WRITE_TIMEOUT = 30.0


class SerialTransport(ByteTransport):
    """Byte stream over a :mod:`pyserial` port.

    The Videomancer ignores the baud rate for USB CDC; it is accepted only
    because pyserial requires one.
    """

    def __init__(self, port, baudrate=115200, write_timeout=WRITE_TIMEOUT):
        try:
            import serial
        except ImportError as err:
            raise TransportUnavailableError("pyserial is not installed") from err
        self._port_name = port
        try:
            self._port = serial.Serial(port, baudrate=baudrate, timeout=0, write_timeout=write_timeout)
        except Exception as err:
            raise TransportUnavailableError(f"cannot open {port}: {err}") from err

    @property
    def name(self):
        """Name of the underlying serial port."""
        return self._port_name

    def write(self, data):
        """Write bytes to the port."""
        if self._port is None:
            raise TransportError("transport is closed")
        try:
            self._port.write(bytes(data))
            self._port.flush()
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
