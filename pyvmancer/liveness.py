"""Waiting on device-side work for as long as the device is alive, rather than to a deadline.

A command that keeps the shell busy with device-side work (a program load
reading its bitstream from the card, an author enumeration, a hash) gives no
reply until it is done, and how long that takes is the device's business. The
wait therefore ends only on positive evidence: the reply, an error reply, a
transport error, or the device leaving USB. A :class:`Watch` carries that
bookkeeping for one operation and reports progress at a display cadence that
never ends the wait. Its optional stall guard is for unattended runs that would
rather fail than wait indefinitely; it is off unless the caller sets it.
"""

import time

from .errors import DeviceGoneError, StallError

#: Seconds between progress reports; a display cadence, never a deadline.
REPORT_S = 2.0
#: Seconds one read or write blocks before liveness is checked again.
POLL_S = 0.2

_USB = {True: "usb up", False: "usb gone", None: "usb unknown"}


class Watch:
    """Liveness of one long device operation: progress, USB presence and the opt-in stall guard.

    ``present`` answers True while the device is enumerated, False once it is
    gone and None where presence cannot be read. Each :meth:`observe` names
    what the wait is seeing, ``label`` for display and ``evidence`` for
    change detection; any change restarts the stall clock.
    """

    def __init__(self, what, present, *, report=None, interval=REPORT_S, stall=None, clock=time.monotonic):
        self.what = what
        self._present = present
        self._report = report
        self.interval = interval
        self.stall = stall
        self._clock = clock
        self.start = self.changed = clock()
        self._due = self.start + interval
        self.state = (None, None)

    def describe(self, now, attached):
        """``<label> <seconds in it>s, usb <state>``."""
        return f"{self.state[0]} {now - self.changed:.1f}s, {_USB[attached]}"

    def observe(self, label, evidence=None):
        """Record what the wait sees, then raise if the device has gone or the stall guard trips."""
        now = self._clock()
        if (label, evidence) != self.state:
            self.state, self.changed = (label, evidence), now
        attached = self._present()
        if attached is False:
            raise DeviceGoneError(self.what, self.describe(now, attached))
        if self.stall is not None and now - self.changed >= self.stall:
            raise StallError(self.what, self.describe(now, attached), self.stall)
        if self._report is not None and now >= self._due:
            self._due = now + self.interval
            self._report(f"still {self.what} after {now - self.start:.1f}s: {self.describe(now, attached)}")
