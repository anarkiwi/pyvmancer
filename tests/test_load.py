"""A program load is complete only once ``fpga status`` and ``program info`` say so."""

import json

import pytest

from pyvmancer.errors import ProgramLoadError, ShellError
from pyvmancer.shell import LOAD_POLL_S, fpga_running

from .test_shell import legacy

WAIT = ["fpga status", "program info"]


@pytest.mark.parametrize(
    "transitional",
    [
        ["!3:no program loaded"],
        ["@fpga:" + json.dumps({"state": "configuring", "configured": False, "program": "Passthru"})],
        ["@fpga:" + json.dumps({"state": "running", "configured": True, "program": "Roll Shutter"})] * 3,
    ],
)
def test_load_waits_for_the_device_to_run_the_program(shell, byte_transport, transitional, monkeypatch):
    """A load is not complete until ``fpga status`` runs the program and ``program info`` answers."""
    legacy(byte_transport)
    slept = []
    monkeypatch.setattr("pyvmancer.shell.time.sleep", slept.append)
    byte_transport.queue("program info" if transitional[0].startswith("!") else "fpga status", *transitional)
    shell.load_program("Passthru", settle=0)
    polls = byte_transport.written[byte_transport.written.index("program load Passthru") + 1 :]
    assert polls.count("fpga status") == len(transitional) + 1
    assert polls[-2:] == WAIT and slept == [LOAD_POLL_S] * len(transitional)


def test_load_that_never_completes_names_the_last_status(shell, byte_transport):
    """Past the shell's reply deadline the wait raises with the last ``fpga status``."""
    byte_transport.on("fpga status", json.dumps({"state": "error", "configured": False, "program": None}))
    clock = iter(range(100)).__next__
    slept = []
    shell.timeout = 3
    with pytest.raises(
        ProgramLoadError, match="Passthru did not finish.*state error, configured False"
    ) as err:
        shell.wait_loaded("Passthru", sleep=slept.append, clock=clock)
    assert err.value.status["state"] == "error" and byte_transport.written.count("fpga status") == 3
    assert len(slept) == 2


def test_wait_loaded_raises_other_program_info_errors(shell, byte_transport):
    """Only "no program loaded" means a load in progress; any other error is the device's answer."""
    byte_transport.on_error("program info", 7, "io error")
    with pytest.raises(ShellError, match="io error"):
        shell.wait_loaded()


@pytest.mark.parametrize(
    "status,program,running",
    [
        ({}, "Perlin", True),
        ({"state": "running", "configured": True, "program": "Perlin"}, "perlin", True),
        ({"state": "running", "configured": True, "program": "Perlin"}, None, True),
        ({"state": "running", "configured": True, "program": None}, "Perlin", False),
        ({"state": "running", "configured": False, "program": "Perlin"}, "Perlin", False),
        ({"state": "idle", "configured": True, "program": "Perlin"}, "Perlin", False),
    ],
)
def test_fpga_running(status, program, running):
    """Running means configured, in the running state, and on the requested program."""
    assert fpga_running(status, program) is running
