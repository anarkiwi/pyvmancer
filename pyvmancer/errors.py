"""Exception hierarchy for pyvmancer."""

from .const import decode_error_code


class VmancerError(Exception):
    """Base class for all pyvmancer errors."""


class DeviceNotFoundError(VmancerError):
    """No Videomancer matched the requested selector."""


class TransportError(VmancerError):
    """Underlying MIDI/serial transport failed."""


class TransportUnavailableError(TransportError):
    """Transport backend is not usable (missing library, permissions, no device node)."""


class PortBusyError(TransportUnavailableError):
    """Another process holds the serial port; the shell serves one connection at a time."""

    def __init__(self, port):
        self.port = port
        super().__init__(
            f"{port} is held by another process; the Videomancer shell serves one connection at a time, "
            f"so close the other client (e.g. find it with `fuser {port}`)"
        )


class FirmwareError(VmancerError):
    """A firmware release could not be resolved, downloaded, or flashed."""


class ShellError(VmancerError):
    """Device returned a ``!<code>:<message>`` response.

    ``code`` is the 32-bit code, ``domain << 26 | item`` as the serial port guide lays it out;
    it is ``None`` when the device sent an unparseable error line.
    """

    def __init__(self, code, message, command=None):
        self.code = code
        self.message = message
        self.command = command
        detail = f"{command!r}: " if command else ""
        where = "" if code is None else f" (domain {self.domain}, item {self.item})"
        super().__init__(f"{detail}[{code}{where}] {message}")

    @property
    def domain(self):
        """The firmware domain that raised the error, or None."""
        return None if self.code is None else decode_error_code(self.code)[0]

    @property
    def item(self):
        """The error within its domain, or None."""
        return None if self.code is None else decode_error_code(self.code)[1]


class ReplyShapeError(VmancerError):
    """A JSON reply lacks the fields its command answers with: it is not this command's reply."""

    def __init__(self, tag, payload, missing):
        self.tag = tag
        self.payload = payload
        self.missing = tuple(missing)
        want = f"fields {', '.join(self.missing)} as documented" if self.missing else "an object"
        super().__init__(f"reply @{tag} lacks {want}: {payload[:200]!r}")


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
