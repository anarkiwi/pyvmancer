"""Loads and other device-side work are awaited on liveness: never a deadline, only positive evidence."""

import json

import pytest

from pyvmancer.const import ShellErrorCode

from pyvmancer.errors import DeviceGoneError, ProgramLoadError, ShellError, StallError
from pyvmancer.shell import LOAD_POLL_S, fpga_running

from .conftest import reply_line
from .test_shell import legacy, multi_author

WAIT = ["fpga status", "program info"]


def fpga(state="running", configured=True, program="Fray"):
    """An ``fpga status`` reply line."""
    return reply_line("fpga", json.dumps({"state": state, "configured": configured, "program": program}))


@pytest.mark.parametrize(
    "transitional",
    [
        [f"!{int(ShellErrorCode.SERVICE_UNAVAILABLE)}:no program loaded"],
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


def test_a_busy_shell_is_waited_on_past_any_deadline(shell, byte_transport, clocked):
    """The shell answers nothing while it reads the program; nothing is resent and progress is reported."""
    multi_author(byte_transport, {"lzx": 0, "czartroniks": 0})
    shell.sync()
    byte_transport.hold("program load Fray", 30)
    sent = []
    tick = byte_transport.tick
    byte_transport.tick = lambda: sent.append(len(byte_transport.written)) or tick()
    shell.load_program("czartroniks/Fray", settle=0)
    loading = byte_transport.written.index("program load Fray")
    assert sent.count(loading + 1) >= 30
    assert byte_transport.written[loading + 1 :] == WAIT
    assert (
        clocked[0]
        == "still loading czartroniks/Fray after 4.0s: shell busy on 'program load Fray' 2.0s, usb up"
    )
    assert len(clocked) == 7 and all("shell busy" in line for line in clocked)


def test_an_idle_fpga_with_no_program_is_still_configuring(shell, byte_transport, clocked):
    """``state idle, program ''`` after the ``ok`` is the reconfiguration under way, reported as such."""
    legacy(byte_transport)
    byte_transport.queue("fpga status", *[fpga("idle", True, "")] * 12)
    shell.load_program("Fray", settle=0)
    assert byte_transport.written.count("fpga status") == 13
    assert clocked and all(": fpga idle " in line for line in clocked)


def test_unanswered_polls_are_a_busy_shell_not_a_dead_one(shell, byte_transport, clocked):
    """A poll the device sits on is awaited like the load itself."""
    legacy(byte_transport)
    byte_transport.queue("fpga status", fpga("idle", True, ""))
    byte_transport.on("fpga status", json.dumps({"state": "running", "configured": True, "program": "Fray"}))
    byte_transport.hold("fpga status", 12)
    shell.load_program("Fray", settle=0)
    assert any("shell busy" in line for line in clocked)


def test_losing_usb_mid_load_fails_at_once(shell, byte_transport):
    """The device leaving USB is the one thing that ends a busy wait early."""
    legacy(byte_transport)
    shell.sync()
    byte_transport.reads = 0
    byte_transport.hold("program load Fray", 100)
    byte_transport.presence = [True, True]
    byte_transport.attached = False
    with pytest.raises(DeviceGoneError, match="while loading Fray .shell busy"):
        shell.load_program("Fray", settle=0)
    assert byte_transport.reads == 4


def test_an_error_reply_is_the_device_refusing(shell, byte_transport):
    """Any error reply to ``program load`` is a real failure carrying the device's message."""
    legacy(byte_transport)
    byte_transport.on_error("program load Nope", ShellErrorCode.BUFFER_OVERFLOW, "program not found")
    with pytest.raises(ShellError, match="program not found"):
        shell.load_program("Nope", settle=0)


def test_reverting_to_the_old_program_is_a_failed_load(shell, byte_transport, clocked):
    """Once the FPGA has left its first state, settling on another program is positive evidence."""
    del clocked
    legacy(byte_transport)
    byte_transport.queue(
        "fpga status", fpga(program="Passthru"), fpga("idle", True, ""), fpga(program="Passthru")
    )
    with pytest.raises(ProgramLoadError, match="Fray did not load: the device runs 'Passthru' instead"):
        shell.load_program("Fray", settle=0)


def test_the_stall_guard_fails_only_when_opted_in(shell, byte_transport, clocked):
    """An FPGA idle without end waits on, unless the caller set a stall period."""
    legacy(byte_transport)
    byte_transport.queue("fpga status", *[fpga("idle", True, "")] * 200)
    shell.load_program("Fray", settle=0)
    assert byte_transport.written.count("fpga status") == 201
    shell.stall = 30.0
    byte_transport.on("fpga status", json.dumps({"state": "idle", "configured": True, "program": ""}))
    with pytest.raises(StallError, match="nothing changed for 30.0s while loading Passthru .fpga idle"):
        shell.load_program("Passthru", settle=0)
    assert clocked


def test_wait_loaded_raises_other_program_info_errors(shell, byte_transport):
    """Only "no program loaded" means a load in progress; any other error is the device's answer."""
    byte_transport.on_error("program info", ShellErrorCode.IO_ERROR, "io error")
    with pytest.raises(ShellError, match="io error"):
        shell.wait_loaded()


@pytest.mark.parametrize(
    "command,call",
    [
        ("programs enumerate czartroniks", lambda shell: shell.enumerate_author("czartroniks")),
        ("fs hash sd:/p.vmprog", lambda shell: shell.hash_file("sd:/p.vmprog")),
        ("settings export", lambda shell: shell.export_settings()),
        ("settings import {}", lambda shell: shell.import_settings({})),
        ("settings reset", lambda shell: shell.reset_settings()),
        ("program presets apply 2 factory", lambda shell: shell.apply_preset(2, "factory")),
        ("program presets apply Warm", lambda shell: shell.apply_preset("Warm")),
    ],
)
def test_device_side_work_waits_on_liveness(shell, byte_transport, clocked, command, call):
    """Each of these keeps the shell busy with card or flash work, so none has a deadline."""
    del clocked
    byte_transport.on(command, "ok" if command.split()[0] != "fs" else "{}")
    if command.split()[:2] == ["programs", "enumerate"]:
        byte_transport.on(command, '{"author":"czartroniks","count":0}')
    if command.split()[:2] == ["settings", "export"]:
        byte_transport.on(command, "{}")
    byte_transport.hold(command, 50)
    call(shell)
    assert byte_transport.reads > 50


def test_ordinary_commands_wait_on_liveness_too(shell, byte_transport, clocked):
    """A quick command the device sits on is waited on past any deadline, and nothing else is sent."""
    del clocked
    byte_transport.on("version", "1.0.1")
    byte_transport.hold("version", 50)
    assert shell.version() == "1.0.1"
    assert byte_transport.reads > 50 and byte_transport.written == ["version"]


def test_put_waits_on_liveness_for_the_handshake_and_the_commit(shell, byte_transport, clocked):
    """The device commits a put to the card at its own pace."""
    del clocked
    byte_transport.on("fs caps", json.dumps({"put_max_bytes": 1 << 20}))
    lines = [reply_line("fs", json.dumps({"put": "ready"})), reply_line("fs", json.dumps({"written": 3}))]
    byte_transport.on_lines("fs put sd:/p.bin 3", lines)
    byte_transport.hold("fs put sd:/p.bin 3", 50)
    shell.put_file("sd:/p.bin", b"abc")
    assert byte_transport.reads > 50


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
