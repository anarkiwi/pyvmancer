"""Read-only state watching: change lines named from the guide's key table, and reboot detection."""

import json
from datetime import datetime

import pytest

from pyvmancer.const import ShellErrorCode
from pyvmancer.errors import VmancerError
from pyvmancer.shell import ShellClient
from pyvmancer.statewatch import WATCHED_KEYS, StateWatch, changes, describe, rebooted, snapshot

from .conftest import FakeByteTransport

READ_ONLY = ("settings get", "fpga status", "transport status", "modulation cc-map", "program state")


class Device:
    """Device state behind any number of FakeByteTransports, one per connection."""

    def __init__(self):
        self.settings = {1: 4, 4: 1, 5: 0, 7: "Cascade", 8: 0, 9: 41, 10: 0, 11: 12000, 19: "lzx"}
        self.fpga = {"state": "running", "program": "Cascade", "browse_author": "lzx"}
        self.loaded = True
        self.transports = []
        self.reconnect = None

    def connect(self):
        """A fresh client on a transport answering from the current state."""
        transport = FakeByteTransport()
        for key in WATCHED_KEYS:
            if key in self.settings:
                payload = json.dumps({"key": int(key), "value": self.settings[key]})
                transport.on(f"settings get {int(key)}", payload)
        transport.on("fpga status", json.dumps(self.fpga))
        transport.on("transport status", json.dumps({"state": "stopped", "bpm_x100": self.settings[11]}))
        if self.loaded:
            transport.on("program state", json.dumps({"m": [512] * 12}))
        else:
            transport.on_error("program state", ShellErrorCode.SERVICE_UNAVAILABLE, "no program loaded")
        self.transports.append(transport)
        return ShellClient(transport)


def _run(device, steps, seconds=3.0):
    """Run a watch whose n-th sleep applies ``steps[n]`` to the device; returns the watch and its lines."""
    lines, now = [], [0.0]

    def sleep(interval):
        """Advance the clock and change the device as scheduled."""
        now[0] += interval
        step = steps.pop(0) if steps else None
        if step is not None:
            step(device)

    class Bar:
        """A tqdm stand-in."""

        total, n = round(seconds), 0

        def update(self, count):
            """Advance."""
            self.n += count

    watch = StateWatch(
        device.reconnect or device.connect,
        emit=lines.append,
        clock=lambda: now[0],
        sleep=sleep,
        now=lambda: datetime(2026, 11, 16),
    )
    progress = Bar()
    watch.run(seconds, 1.0, progress=progress)
    assert progress.n == progress.total
    return watch, lines


def _rescript(device):
    """Re-answer the current connection from the device's state."""
    device.transports[-1].scripts.update(device.connect().transport.scripts)
    device.transports.pop()


def test_snapshot_reads_only_and_names_absent_keys():
    """Only reads are sent; a key the store lacks and an unloaded program read as None."""
    device = Device()
    device.loaded = False
    shell = device.connect()
    del device.transports[-1].cc.keys[11]
    device.transports[-1].on_error("settings get 211", ShellErrorCode.FILE_NOT_FOUND, "setting not found")
    state = snapshot(shell)
    assert state["settings 211"] is None and state["program state m"] is None
    assert state["settings 7"] == "Cascade" and state["fpga browse_author"] == "lzx"
    assert all(line.startswith(READ_ONLY) for line in device.transports[-1].written)


def test_snapshot_raises_on_other_errors():
    """An error other than an absent key is not read as absence."""
    device = Device()
    shell = device.connect()
    device.transports[-1].on_error("settings get 9", ShellErrorCode.INVALID_STATE, "busy")
    with pytest.raises(VmancerError, match="busy"):
        snapshot(shell)


def test_changes_are_named_from_the_key_table():
    """Each change prints its key's name and the guide's value names."""
    device = Device()

    def author_switch(dev):
        """The device re-indexes another author on its own."""
        dev.settings[19] = "boneoh"
        dev.settings[1] = 1
        dev.fpga["browse_author"] = "boneoh"
        _rescript(dev)

    watch, lines = _run(device, [None, author_switch])
    body = [line.split("s ", 1)[1] for line in lines[1:]]
    assert body == [
        "settings 1 (Video Route Mode): 4 (Auto) -> 1 (HDMI In)",
        "settings 19 (program author): 'lzx' -> 'boneoh'",
        "fpga browse_author: 'lzx' -> 'boneoh'",
    ]
    assert lines[0].startswith(
        "2026-11-16T00:00:00.000 +     0.0s start settings 1 (Video Route Mode): 4 (Auto)"
    )
    assert watch.changed == 3 and watch.reboots == 0


def test_a_rising_boot_count_is_a_reboot():
    """Boot count key 9 increasing is flagged as a reboot."""
    device = Device()

    def reboot(dev):
        """The device reboots between polls."""
        dev.settings[9] = 42
        _rescript(dev)

    watch, lines = _run(device, [reboot])
    assert "settings 9 (boot count): 41 -> 42" in lines[1]
    assert lines[2].endswith("REBOOT (boot count rose): settings 9 (boot count): 42")
    assert watch.reboots == 1


def test_usb_re_enumeration_reconnects_and_reports():
    """The device leaving USB mid-poll is reported, the link reopened, and the reboot flagged."""
    device = Device()
    attempts = []

    def reconnect():
        """The first reopen after the drop fails while the device is still enumerating."""
        attempts.append(1)
        if len(attempts) == 2:
            raise VmancerError("no device")
        return device.connect()

    device.reconnect = reconnect

    def drop(dev):
        """The device leaves USB and comes back having rebooted."""
        dev.transports[-1].attached = False
        dev.transports[-1].silence("settings get 1")
        dev.settings[9] = 42

    watch, lines = _run(device, [drop], seconds=4.0)
    assert any("link lost (device left USB" in line for line in lines)
    reboot = [line for line in lines if "REBOOT" in line]
    assert reboot == [reboot[0]] and "REBOOT (boot count rose, USB re-enumeration)" in reboot[0]
    assert watch.reboots == 1 and len(attempts) == 3
    assert device.transports[0].closed


def test_reconnect_gives_up_when_time_runs_out():
    """A device that never comes back ends the watch at its time limit."""
    device = Device()
    calls = []

    def reconnect():
        """Only the first open succeeds."""
        calls.append(1)
        if len(calls) > 1:
            raise VmancerError("gone")
        return device.connect()

    device.reconnect = reconnect

    def drop(dev):
        """The device leaves USB for good."""
        dev.transports[-1].attached = False
        dev.transports[-1].silence("settings get 1")

    _, lines = _run(device, [drop], seconds=3.0)
    assert "link lost" in lines[-1]


def test_helpers():
    """Changes compare labels, reboots need two integer boot counts, absent values are named."""
    assert changes({"a": 1, "b": 2}, {"a": 1, "b": 3}) == [("b", 2, 3)]
    assert not rebooted({"settings 9": None}, {"settings 9": 3})
    assert describe("settings 211", None) == "settings 211 (MIDI Assign P12): absent"
    assert describe("settings 200", 130816) == "settings 200 (MIDI Assign P1): 130816 (CC 0 auto)"


def test_cli_watch_prints_the_start_state(monkeypatch, capsys):
    """``vmancer watch`` opens the shell read-only and prints the starting state."""
    from pyvmancer import cli

    device = Device()
    monkeypatch.setattr(cli, "open_shell", lambda **_: device.connect())
    assert cli.main(["watch", "--seconds", "0"]) == 0
    out, err = capsys.readouterr()
    assert "start settings 1 (Video Route Mode): 4 (Auto)" in out and "0 changes, 0 reboots" in err
