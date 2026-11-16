"""Program listings checked against the device's own counts, and replies from another client set aside."""

import pytest

from pyvmancer.const import PresetErrorCode, ShellErrorCode
from pyvmancer.errors import InterleavedReplyError, ProgramListError, ReplyShapeError, ShellError, StallError
from pyvmancer.shell import listing_problems

from .test_shell import multi_author

# pylint: disable=protected-access


def test_listing_always_enumerates_for_its_count(shell, byte_transport):
    """A listing enumerates every time: the reply's count is what the names are checked against."""
    multi_author(byte_transport, {"lzx": 0})
    byte_transport.on("programs enumerate lzx", '{"author":"lzx","skipped":true,"count":1}')
    byte_transport.on("programs list", '{"programs":["a"]}')
    assert shell.programs("lzx") == shell.programs("lzx") == ["a"]
    assert byte_transport.written.count("programs enumerate lzx") == 2


def test_enumerate_skips_only_when_the_device_reports_the_author_active(shell, byte_transport):
    """The skip trusts the device's ``browse_author``, which another client can change."""
    multi_author(byte_transport, {"lzx": 0, "czartroniks": 0})
    shell.enumerate_author("czartroniks")
    shell.enumerate_author("czartroniks")
    assert byte_transport.written.count("programs enumerate czartroniks") == 1
    byte_transport._author = "lzx"
    shell.enumerate_author("czartroniks")
    assert byte_transport.written.count("programs enumerate czartroniks") == 2


def test_enumerate_without_browse_author_always_sends(shell, byte_transport):
    """A device that does not report its active author is enumerated every time."""
    byte_transport.on("fpga status", '{"state":"running"}')
    byte_transport.on("programs enumerate lzx", '{"author":"lzx","count":0}')
    shell.enumerate_author("lzx")
    shell.enumerate_author("lzx")
    assert byte_transport.written.count("programs enumerate lzx") == 2


@pytest.mark.parametrize(
    "error",
    [
        (ShellErrorCode.SERVICE_UNAVAILABLE, "busy"),
        (ShellErrorCode.BUFFER_OVERFLOW, "buffer overflow"),
        (PresetErrorCode.INVALID_INDEX, "a preset domain item 2 is no usage error"),
    ],
)
def test_authors_failure_is_never_read_as_legacy_firmware(shell, byte_transport, error):
    """Only an unknown-command or usage error means no authors; anything else raises."""
    byte_transport.on_error("programs authors", *error)
    byte_transport.on("programs list", '{"programs":["Quilt"]}')
    with pytest.raises(ShellError, match=error[1]):
        shell.programs()
    assert "programs list" not in byte_transport.written


def test_silent_authors_stalls_rather_than_reading_as_absent(shell, byte_transport, clocked):
    """A silent ``programs authors`` is waited on; only the opt-in stall guard ends it, and it raises."""
    del clocked
    byte_transport.silence("programs authors")
    shell.stall = 1.0
    with pytest.raises(StallError):
        shell.programs()
    assert "programs list" not in byte_transport.written


@pytest.mark.parametrize(
    "payload", ['["lzx"]', '{"count":1}', '{"authors":"lzx"}', '{"count":40,"programs":["a"]}']
)
def test_authors_of_the_wrong_shape_raise(shell, byte_transport, payload):
    """A reply that is not an authors object raises instead of reading as no authors."""
    byte_transport.on("programs authors", payload)
    with pytest.raises(ReplyShapeError, match="authors"):
        shell.authors()


@pytest.mark.parametrize("code", [ShellErrorCode.UNKNOWN_COMMAND, ShellErrorCode.PARSE_ERROR])
def test_absent_authors_command_lists_one_set(shell, byte_transport, code):
    """Unknown command and usage errors are the firmware lacking ``programs authors``."""
    byte_transport.on_error("programs authors", code, "unknown command")
    byte_transport.on("programs list", '{"programs":["a"]}')
    assert shell.programs() == ["a"]


def test_short_listing_is_relisted_then_accepted(shell, byte_transport):
    """A listing short of the enumerated count is re-enumerated and re-listed once."""
    multi_author(byte_transport, {"czartroniks": 0})
    byte_transport.on("programs enumerate czartroniks", '{"author":"czartroniks","count":2}')
    byte_transport.queue("programs list", '@programs:{"programs":["Quilt"]}')
    byte_transport.on("programs list", '{"programs":["Quilt","Fray"]}')
    assert shell.programs() == ["czartroniks/Quilt", "czartroniks/Fray"]
    assert byte_transport.written.count("programs enumerate czartroniks") == 2


@pytest.mark.parametrize(
    "enumerated,listing,want",
    [
        (
            '{"author":"lzx","count":3}',
            '{"programs":["a","b"]}',
            "listed 2 names against an enumerated count of 3",
        ),
        ('{"author":"lzx","count":2}', '{"programs":["a","a"]}', "repeated ['a']"),
        ('{"author":"lzx","count":1}', '{"programs":["x/a"]}', "names with a '/' ['x/a']"),
        ('{"author":"czartroniks","count":1}', '{"programs":["a"]}', "enumerate answered for 'czartroniks'"),
    ],
)
def test_inconsistent_listing_raises_with_both_attempts(shell, byte_transport, enumerated, listing, want):
    """A listing still inconsistent after one re-enumeration raises naming both attempts."""
    multi_author(byte_transport, {"lzx": 0})
    byte_transport.on("programs enumerate lzx", enumerated)
    byte_transport.on("programs list", listing)
    with pytest.raises(ProgramListError) as caught:
        shell.programs()
    assert caught.value.problems == (want, want) and want in str(caught.value)
    assert byte_transport.written.count("programs enumerate lzx") == 2


def test_author_switched_by_another_client_is_a_listing_problem():
    """The device's active author after listing must be the one listed."""
    problem = listing_problems("czartroniks", {"author": "czartroniks"}, ["Quilt"], active="lzx")
    assert problem == "the device's active author is 'lzx'"
    assert not listing_problems("lzx", {}, ["a", "b"], active=None)


def test_foreign_reply_is_set_aside(shell, byte_transport):
    """A reply tagged for another command group answers another client: it is kept aside, not returned."""
    byte_transport.on_lines("programs list", ['@modulation:{"active":[]}', '@programs:{"programs":["a"]}'])
    assert shell.command("programs", "list").json() == {"programs": ["a"]}
    assert shell.foreign_lines == ['@modulation:{"active":[]}']


def test_unknown_tag_is_still_this_commands_reply(shell, byte_transport):
    """A tag outside the documented groups is not evidence of another client."""
    byte_transport.on_lines("version", ["@ver:1.0"])
    assert shell.version() == "1.0"


@pytest.mark.parametrize(
    "line",
    [
        '@modulation:{"ac@programs:{"author":"czartroniks","count":107}',
        '{"ac@programs:{"author":"czartroniks","count":107}',
        '@programs:{"programs":["a"]!1476395010:usage',
    ],
)
def test_spliced_reply_raises(shell, byte_transport, line):
    """A line with another reply spliced into it is flagged, never parsed as this command's reply."""
    byte_transport.on_lines("programs list", [line, '@programs:{"programs":[]}'])
    with pytest.raises(InterleavedReplyError, match="other client"):
        shell.command("programs", "list")
