"""One command at a time: replies are never attributed to a later command, whatever was abandoned."""

import json

import pytest

from pyvmancer.const import ShellErrorCode
from pyvmancer.errors import InterleavedReplyError, ReplyShapeError, ShellError, StallError
from pyvmancer.shell import BARRIER_WORD, ShellClient

from .conftest import FakeByteTransport, error_line, reply_line

LZX_LIST = json.dumps({"count": 2, "programs": ["Afterdark", "Alcove"]})
AUTHORS = json.dumps({"count": 2, "authors": ["lzx", "boneoh"]})

# pylint: disable=protected-access


def _stalled(shell, byte_transport, command, reads):
    """Hold ``command``'s reply for ``reads`` reads and let the stall guard abandon the wait."""
    now = [0.0]
    shell.clock = lambda: now[0]
    byte_transport.tick = lambda: now.__setitem__(0, now[0] + 1.0)
    shell.stall = 3.0
    byte_transport.hold(command, reads)
    with pytest.raises(StallError):
        shell.command(*command.split(" "))
    shell.stall = None
    assert not shell.synced


def test_a_late_reply_after_a_busy_period_is_never_the_next_commands(shell, byte_transport):
    """A ``programs list`` answered after its wait was abandoned is discarded, not read as the authors."""
    byte_transport.on("programs list", LZX_LIST).on("programs authors", AUTHORS)
    _stalled(shell, byte_transport, "programs list", 10)
    assert shell.authors() == ["lzx", "boneoh"]
    assert reply_line("programs", LZX_LIST) in shell.discarded
    assert byte_transport.written == ["programs list", "programs authors"]
    assert len(byte_transport.barriers) == 2 and shell.synced


def test_nothing_is_sent_while_a_reply_is_outstanding(shell, byte_transport):
    """However long the device sits on a command, the next is written only after its reply."""
    byte_transport.on("programs list", LZX_LIST)
    shell.sync()
    byte_transport.hold("programs list", 40)
    lengths = []
    byte_transport.tick = lambda: lengths.append(len(byte_transport.written))
    assert shell.command("programs", "list").shaped("programs")["programs"] == ["Afterdark", "Alcove"]
    shell.version()
    assert lengths.count(1) > 40 and byte_transport.written == ["programs list", "version"]


def test_an_interrupted_wait_is_resynchronised(shell, byte_transport):
    """A KeyboardInterrupt mid-wait leaves the client unsynced; the next command drops the stale reply."""
    byte_transport.on("status", '{"product":"Videomancer"}').on("version", "1.0.1")
    shell.sync()
    byte_transport.hold("status", 5)

    def interrupt():
        """The user presses Ctrl-C while the shell is busy."""
        byte_transport.tick = None
        raise KeyboardInterrupt

    byte_transport.tick = interrupt
    with pytest.raises(KeyboardInterrupt):
        shell.status()
    assert not shell.synced
    assert shell.version() == "1.0.1"
    assert shell.discarded == [reply_line("status", '{"product":"Videomancer"}')]


def test_a_new_client_discards_what_a_previous_one_left_owed():
    """A reply still in flight from another process's abandoned command never answers this one."""
    transport = FakeByteTransport()
    transport.on("programs authors", AUTHORS)
    transport._pending.extend(f"{reply_line('programs', LZX_LIST)}\n".encode())
    shell = ShellClient(transport)
    assert not shell.synced
    assert shell.authors() == ["lzx", "boneoh"]
    assert shell.discarded == [reply_line("programs", LZX_LIST)]


def test_only_this_barriers_echo_ends_the_sync(shell, byte_transport):
    """An echo of an older barrier is stale output like any other, and is skipped."""
    old = error_line(int(ShellErrorCode.UNKNOWN_COMMAND), f"unknown command: {BARRIER_WORD}0123")
    byte_transport._pending.extend(f"{old}\n".encode())
    byte_transport.on("version", "1.0.1")
    assert shell.version() == "1.0.1"
    assert shell.discarded == [old]
    assert byte_transport.barriers[0].startswith(BARRIER_WORD) and byte_transport.barriers[0] not in old


def test_an_error_reply_completes_the_exchange(shell, byte_transport):
    """A ``!`` answer is the command's reply, so no resync follows it."""
    byte_transport.on_error("program info", ShellErrorCode.SERVICE_UNAVAILABLE, "no program loaded")
    with pytest.raises(ShellError):
        shell.program_info()
    assert shell.synced
    shell.version()
    assert len(byte_transport.barriers) == 1


def test_a_spliced_line_leaves_the_client_unsynced(shell, byte_transport):
    """A line with another reply spliced in is a corrupted exchange; the next command resyncs."""
    byte_transport.on_lines("status", ['{"ac@programs:{"author":"lzx"}'])
    with pytest.raises(InterleavedReplyError):
        shell.status()
    assert not shell.synced
    shell.version()
    assert len(byte_transport.barriers) == 2


def test_unsolicited_lines_are_not_replies(shell, byte_transport):
    """``@program:invalidated:variant_mismatch`` arriving before ``program info`` is set aside."""
    info = json.dumps({"name": "Cascade", "id": "cascade", "parameters": []})
    byte_transport.on_lines(
        "program info", ["@program:invalidated:variant_mismatch", reply_line("program", info)]
    )
    assert shell.program_info()["name"] == "Cascade"
    assert shell.unsolicited == ["@program:invalidated:variant_mismatch"]


def test_cpu_returns_every_core(shell, byte_transport):
    """``cpu`` answers one line per core; all are merged, and the next command gets its own reply."""
    core0 = {"core0": {"usage": 12, "isr": 3}}
    core1 = {"core1": {"available": False}}
    byte_transport.on_lines(
        "cpu", [reply_line("cpu", json.dumps(core0)), "log", reply_line("cpu", json.dumps(core1))]
    )
    byte_transport.on("version", "1.0.1")
    assert shell.cpu() == {**core0, **core1}
    assert shell.version() == "1.0.1"
    assert byte_transport.written == ["cpu", "version"] and len(byte_transport.barriers) == 2


def test_cpu_line_of_the_wrong_shape_raises(shell, byte_transport):
    """A core line that is not an object raises rather than merging."""
    byte_transport.on_lines("cpu", [reply_line("cpu", "[1]")])
    with pytest.raises(ReplyShapeError):
        shell.cpu()


@pytest.mark.parametrize(
    "command,payload,call",
    [
        ("programs enumerate lzx", '{"author":"lzx"}', lambda s: s.enumerate_author("lzx", force=True)),
        ("programs enumerate lzx", '["lzx"]', lambda s: s.enumerate_author("lzx", force=True)),
        ("programs list", '["Afterdark"]', lambda s: s.programs(None)),
        ("programs list", '{"programs":"Afterdark"}', lambda s: s.programs(None)),
        (
            "programs enumerate lzx",
            '{"author":"lzx","count":"2"}',
            lambda s: s.enumerate_author("lzx", force=True),
        ),
        ("fpga status", '{"count":2,"authors":[]}', lambda s: s.fpga_status()),
        ("fpga status", "[]", lambda s: s.active_author()),
    ],
)
def test_replies_of_the_wrong_shape_raise(shell, byte_transport, command, payload, call):
    """A reply lacking its command's fields raises ReplyShapeError instead of defaulting."""
    byte_transport.on_error("programs authors", ShellErrorCode.UNKNOWN_COMMAND, "unknown command")
    byte_transport.on(command, payload)
    with pytest.raises(ReplyShapeError):
        call(shell)


def test_an_abandoned_put_leaves_the_client_unsynced(shell, byte_transport):
    """A put whose commit wait is abandoned is resynchronised before anything else is read."""
    byte_transport.on("fs caps", json.dumps({"put_max_bytes": 1 << 20}))
    byte_transport.on("fs put sd:/p.bin 3", json.dumps({"put": "ready"}))
    byte_transport.presence = [True] * 3
    byte_transport.attached = False
    with pytest.raises(Exception, match="left USB"):
        shell.put_file("sd:/p.bin", b"abc")
    assert not shell.synced
