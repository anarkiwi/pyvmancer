"""Exception hierarchy for pyvmancer."""


class VmancerError(Exception):
    """Base class for all pyvmancer errors."""


class DeviceNotFoundError(VmancerError):
    """No Videomancer matched the requested selector."""


class TransportError(VmancerError):
    """Underlying MIDI/serial transport failed."""


class TransportUnavailableError(TransportError):
    """Transport backend is not usable (missing library, permissions, no device node)."""


class FirmwareError(VmancerError):
    """A firmware release could not be resolved, downloaded, or flashed."""


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


class DeviceGoneError(TransportError):
    """The device left USB while an operation was waiting on it."""

    def __init__(self, what, state):
        self.what = what
        self.state = state
        super().__init__(f"device left USB while {what} ({state})")


class StallError(VmancerError):
    """Opt-in stall guard: nothing observable changed for ``stall`` seconds while waiting on the device."""

    def __init__(self, what, state, stall):
        self.what = what
        self.state = state
        self.stall = stall
        super().__init__(f"nothing changed for {stall}s while {what} ({state})")


class ProgramLoadError(VmancerError):
    """A program load ended on something other than the requested program running."""

    def __init__(self, program, status, reason="did not finish loading"):
        self.program = program
        self.status = status
        fields = ", ".join(f"{key} {status.get(key)}" for key in ("state", "configured", "program"))
        super().__init__(f"{program or 'program'} {reason}; last fpga status: {fields}")


class VideoTimingIgnored(VmancerError):
    """``video timing`` acknowledged a standard the output did not take."""

    def __init__(self, requested, actual):
        self.requested = requested
        self.actual = actual
        super().__init__(
            f"video timing {requested} acknowledged but output is {actual}; "
            "check front panel SETTINGS > Vid Route Mode and SYSTEM > Pref. Timing"
        )


class CcMapError(VmancerError):
    """The device's MIDI CC map differs from the CCs the controller sends on."""


class InterleavedReplyError(VmancerError):
    """A reply line carries another reply spliced into it: a second client is using the serial port."""

    def __init__(self, command, line):
        self.command = command
        self.line = line
        super().__init__(
            f"{command!r}: reply {line!r} carries another reply spliced in; close any other client "
            "on the serial port"
        )


class ProgramListError(VmancerError):
    """An author's program listing disagreed with the device's own count, twice."""

    def __init__(self, author, problems):
        self.author = author
        self.problems = tuple(problems)
        super().__init__(
            f"programs of {author!r} are inconsistent: {problems[0]}; re-enumerated: {problems[-1]}"
        )
