"""Exception hierarchy for pyvmancer."""


class VmancerError(Exception):
    """Base class for all pyvmancer errors."""


class DeviceNotFoundError(VmancerError):
    """No Videomancer matched the requested selector."""


class TransportError(VmancerError):
    """Underlying MIDI/serial transport failed."""


class TransportUnavailableError(TransportError):
    """Transport backend is not usable (missing library, permissions, no device node)."""


class ShellError(VmancerError):
    """Device returned a ``!<code>:<message>`` response.

    ``code`` is the numeric error code from the serial command guide; it is
    ``None`` when the device sent an unparseable error line.
    """

    def __init__(self, code, message, command=None):
        self.code = code
        self.message = message
        self.command = command
        detail = f"{command!r}: " if command else ""
        super().__init__(f"{detail}[{code}] {message}")


class ShellTimeoutError(ShellError):
    """Device did not reply to a command within the timeout."""

    def __init__(self, command, timeout):
        super().__init__(None, f"no response within {timeout}s", command)
