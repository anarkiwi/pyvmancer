"""Liveness waits: progress at a display cadence, USB presence, and the opt-in stall guard."""

import pytest

from pyvmancer.errors import DeviceGoneError, StallError
from pyvmancer.liveness import Watch


class Clock:
    """A clock advanced by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_reports_at_the_display_cadence_and_never_ends_the_wait():
    """However long the wait runs, reports only arrive every interval and nothing raises."""
    clock, lines = Clock(), []
    watch = Watch("loading Fray", lambda: True, report=lines.append, interval=2.0, clock=clock)
    for step in range(1, 101):
        clock.now = step * 0.5
        watch.observe("shell busy", (0, 0))
    assert len(lines) == 25
    assert lines[3] == "still loading Fray after 8.0s: shell busy 7.5s, usb up"


def test_a_new_observation_restarts_its_own_clock():
    """The state's duration counts from when it was first seen, the total from the start."""
    clock, lines = Clock(), []
    watch = Watch("loading Fray", lambda: None, report=lines.append, interval=1.0, clock=clock)
    clock.now = 4.0
    watch.observe("fpga idle", ("idle", True, ""))
    clock.now = 5.0
    watch.observe("fpga idle", ("idle", True, ""))
    assert lines == ["still loading Fray after 4.0s: fpga idle 0.0s, usb unknown"] + [
        "still loading Fray after 5.0s: fpga idle 1.0s, usb unknown"
    ]


def test_the_device_leaving_usb_ends_the_wait_at_once():
    """Presence is checked on every observation."""
    watch = Watch("loading Fray", lambda: False, clock=Clock())
    with pytest.raises(
        DeviceGoneError, match="device left USB while loading Fray .shell busy 0.0s, usb gone."
    ):
        watch.observe("shell busy")


def test_the_stall_guard_is_off_by_default():
    """Without a stall setting an unchanging wait never fails."""
    clock = Clock()
    watch = Watch("hashing", lambda: True, clock=clock)
    clock.now = 1e9
    watch.observe("shell busy")


def test_the_stall_guard_trips_only_on_an_unchanged_observable():
    """Change restarts the stall clock; only a whole stall period of sameness fails."""
    clock = Clock()
    watch = Watch("loading Fray", lambda: True, stall=10.0, clock=clock)
    for step, evidence in enumerate((0, 0, 1, 1, 2), start=1):
        clock.now = step * 6.0
        watch.observe("shell busy", evidence)
    clock.now = 39.0
    watch.observe("shell busy", 2)
    clock.now = 40.0
    with pytest.raises(StallError, match="nothing changed for 10.0s while loading Fray"):
        watch.observe("shell busy", 2)
