"""Serial shell protocol parsing and the typed ShellClient command surface."""

import base64
import json

import pytest

from pyvmancer.const import PARAM_COUNT, PresetBank, TransportState
from pyvmancer.errors import ShellError, ShellTimeoutError, VmancerError
from pyvmancer.shell import (
    ERROR_TAG_MASK,
    READ_CHUNK,
    Reply,
    ShellClient,
    decode_error_code,
    encode_preset,
    parse_line,
)

from .conftest import FakeByteTransport

# pylint: disable=protected-access


def test_parse_reply_line():
    """An ``@`` line splits into tag and payload at the first colon."""
    kind, value = parse_line("@version:1.2.3\r\n")
    assert kind == "reply"
    assert (value.tag, value.payload) == ("version", "1.2.3")


def test_parse_reply_payload_may_contain_colons():
    """Only the first colon separates tag from payload."""
    _, value = parse_line('@status:{"a":1}')
    assert value.payload == '{"a":1}'


def test_parse_error_line():
    """A ``!`` line yields a ShellError carrying the decoded code."""
    kind, value = parse_line("!2:parse error\n")
    assert kind == "error"
    assert isinstance(value, ShellError)
    assert (value.code, value.message) == (2, "parse error")


def test_parse_error_line_with_unparseable_code():
    """A non-numeric code leaves the whole body as the message."""
    kind, value = parse_line("!oops:bad thing")
    assert kind == "error"
    assert value.code is None
    assert value.message == "oops:bad thing"


@pytest.mark.parametrize("line", ["hello world", "", "  spaced  "])
def test_parse_log_line(line):
    """Unprefixed lines are log output."""
    assert parse_line(line + "\n") == ("log", line)


@pytest.mark.parametrize(
    "raw,expected", [(1476395010, 2), (ERROR_TAG_MASK | 10, 10), (2, 2), (0, 0), (None, None)]
)
def test_decode_error_code(raw, expected):
    """The firmware error tag is stripped; untagged codes pass through."""
    assert decode_error_code(raw) == expected


def test_reply_ok_json_and_repr():
    """Reply exposes the bare-ok shortcut and JSON decoding."""
    assert Reply("t", "ok").ok
    assert not Reply("t", "nope").ok
    assert Reply("t", '{"a": 1}').json() == {"a": 1}
    assert repr(Reply("t", "p")) == "Reply(tag='t', payload='p')"


def test_reply_json_error():
    """Non-JSON payloads raise a VmancerError naming the tag."""
    with pytest.raises(VmancerError, match="is not JSON"):
        Reply("status", "garbage").json()


def test_shell_error_message_includes_command():
    """Errors raised for a command quote it."""
    err = ShellError(6, "not found", "fs stat sd:/x")
    assert "fs stat sd:/x" in str(err)
    assert "[6] not found" in str(err)


def test_command_joins_parts_and_drops_empties(shell, byte_transport):
    """Parts are space-joined; ``None`` and empty strings are omitted."""
    shell.command("programs", "list", None, "", 3)
    assert byte_transport.last == "programs list 3"
    assert bytes(byte_transport.raw).endswith(b"\n")


def test_command_rejects_newlines(shell):
    """Arguments may not smuggle in extra command lines."""
    with pytest.raises(ValueError, match="must not contain newlines"):
        shell.command("echo", "a\nb")


def test_command_rejects_overlong_line(shell):
    """The device shell has a fixed line buffer."""
    with pytest.raises(ValueError, match="command exceeds"):
        shell.command("x" * 600)


def test_error_reply_raises_shell_error(shell, byte_transport):
    """A ``!`` reply becomes a ShellError naming the originating command."""
    byte_transport.on_error("version", 1, "unknown command")
    with pytest.raises(ShellError) as excinfo:
        shell.version()
    assert excinfo.value.code == 1
    assert excinfo.value.command == "version"


def test_timeout_when_device_is_silent(shell, byte_transport):
    """No reply within the timeout raises ShellTimeoutError."""
    byte_transport.silence("version")
    with pytest.raises(ShellTimeoutError, match="no response within"):
        shell.command("version", timeout=0.05)


def test_log_lines_accumulate(shell, byte_transport):
    """Log output arriving before a reply is collected, dropping blank lines."""
    byte_transport.on_lines("version", ["boot: hello", "", "boot: ready", "@version:9.9"])
    assert shell.version() == "9.9"
    assert shell.log_lines == ["boot: hello", "boot: ready"]


def test_partial_lines_are_buffered(shell, byte_transport):
    """A reply split across reads is reassembled."""
    byte_transport.silence("version")
    byte_transport.write(b"version\n")
    byte_transport._pending.extend(b"@ver")
    assert byte_transport.read() == b"@ver"
    byte_transport._pending.extend(b"@version:1\n")
    assert shell.command("version", timeout=0.5).payload == "1"


def test_version_and_serial(shell, byte_transport):
    """Simple string replies are returned as-is."""
    byte_transport.on("version", "1.4.0").on("serial", "ABC123")
    assert shell.version() == "1.4.0"
    assert shell.serial_number() == "ABC123"
    assert byte_transport.written == ["version", "serial"]


def test_status_decodes_json(shell, byte_transport):
    """``status`` is JSON."""
    byte_transport.on("status", '{"product":"Videomancer","programs":23}')
    assert shell.status()["programs"] == 23


def test_help_and_supports(shell, byte_transport):
    """``help`` is a pipe-separated command list."""
    byte_transport.on("help", "version|status|fs ls")
    assert shell.help() == ["version", "status", "fs ls"]
    assert shell.supports("fs ls")
    assert not shell.supports("nope")


def test_ok_command_rejects_unexpected_payload(shell, byte_transport):
    """A command that must acknowledge with ``ok`` fails loudly otherwise."""
    byte_transport.on("transport play", "busy")
    with pytest.raises(VmancerError, match="expected ok"):
        shell.play()


def test_reboot_bootloader_awaits_nothing(shell, byte_transport):
    """The link drops on reboot, so no reply is read."""
    assert shell.reboot_bootloader() is shell
    assert byte_transport.written == ["reboot bootloader"]


SIMPLE_COMMANDS = [
    ("log_level", ("info",), "log level info"),
    ("language", ("en-US",), "language en-US"),
    ("reset_modulation", (), "modulation reset"),
    ("latch_modulation", (), "modulation latch"),
    ("apply_preset", (2, PresetBank.USER), "program presets apply 2 user"),
    ("apply_preset", ("sunrise",), "program presets apply sunrise"),
    ("rename_preset", (1, "dawn"), "program presets rename 1 dawn"),
    ("set_video_input", ("hdmi",), "video input hdmi"),
    ("set_video_timing", ("1080p30",), "video timing 1080p30"),
    ("fpga_register", (16, 255), "fpga register 16 255"),
    ("remote_enable", (), "remote enable"),
    ("remote_disable", (), "remote disable"),
    ("remote_write", ("knob", 3), "remote write knob 3"),
    ("set_setting", (4, "on"), "settings set 4 on"),
    ("reset_settings", (), "settings reset"),
    ("import_settings", ({"a": 1},), 'settings import {"a":1}'),
    ("set_bpm", (128.5,), "transport bpm 12850"),
    ("play", (), "transport play"),
    ("stop", (), "transport stop"),
    ("midi_monitor", (), "midi monitor on"),
    ("midi_monitor", ("verbose",), "midi monitor verbose"),
    ("msd_enter", (), "msd enter"),
    ("msd_exit", (), "msd exit"),
    ("mkdir", ("sd:/clips",), "fs mkdir sd:/clips"),
    ("remove", ("sd:/clips/a.bin",), "fs rm sd:/clips/a.bin"),
    ("rename", ("sd:/a", "sd:/b"), "fs rename sd:/a sd:/b"),
]


@pytest.mark.parametrize("method,args,expected", SIMPLE_COMMANDS)
def test_ok_commands_emit_expected_line(shell, byte_transport, method, args, expected):
    """Every ``ok``-acknowledged command renders one exact line."""
    assert getattr(shell, method)(*args) is shell
    assert byte_transport.last == expected


JSON_COMMANDS = [
    ("ram", (), "ram"),
    ("cpu", (), "cpu"),
    ("program_info", (), "program info"),
    ("program_state", (), "program state"),
    ("modulation_status", (), "modulation status"),
    ("modulation_audio_status", (), "modulation audio status"),
    ("cc_map", (), "modulation cc-map"),
    ("presets", (), "program presets list"),
    ("get_preset", (0, PresetBank.FACTORY), "program presets get 0 factory"),
    ("delete_preset", (3,), "program presets delete 3"),
    ("video_status", (), "video status"),
    ("fpga_status", (), "fpga status"),
    ("remote_status", (), "remote status"),
    ("msd_status", (), "msd status"),
    ("fs_info", (), "fs info"),
    ("fs_caps", (), "fs caps"),
    ("ls", (), "fs ls sd:/"),
    ("ls", ("sd:/clips",), "fs ls sd:/clips"),
    ("stat", ("sd:/a.bin",), "fs stat sd:/a.bin"),
]


@pytest.mark.parametrize("method,args,expected", JSON_COMMANDS)
def test_json_commands_emit_expected_line(byte_transport, method, args, expected):
    """Every JSON-returning command renders one exact line and decodes its payload."""
    byte_transport.on(expected, '{"ok":true}')
    client = ShellClient(byte_transport, timeout=0.2)
    assert getattr(client, method)(*args) == {"ok": True}
    assert byte_transport.last == expected


def test_language_get(shell, byte_transport):
    """Called with no locale, ``language`` reads the current one."""
    byte_transport.on("language", "en-US")
    assert shell.language() == "en-US"


def test_get_setting(shell, byte_transport):
    """Settings are read back as raw payload strings."""
    byte_transport.on("settings get 7", "42")
    assert shell.get_setting(7) == "42"


def test_save_preset_encodes_fields(shell, byte_transport):
    """Preset fields are appended in the documented wire order."""
    shell.save_preset(1, "dusk", manual=range(PARAM_COUNT), source=[0] * PARAM_COUNT)
    assert byte_transport.last == (
        "program presets save 1 dusk m:0,1,2,3,4,5,6,7,8,9,10,11 sr:0,0,0,0,0,0,0,0,0,0,0,0"
    )


def test_encode_preset_orders_and_prefixes_fields():
    """All five field prefixes are emitted in table order."""
    values = list(range(PARAM_COUNT))
    encoded = encode_preset(manual=values, time=values, space=values, slope=values, source=values)
    assert [part.split(":")[0] for part in encoded.split(" ")] == ["m", "t", "sp", "sl", "sr"]


def test_encode_preset_empty():
    """No fields encode to an empty string."""
    assert encode_preset() == ""


@pytest.mark.parametrize("field", ["manual", "time", "space", "slope", "source"])
def test_encode_preset_length_validation(field):
    """Every field must carry one value per parameter."""
    with pytest.raises(ValueError, match=f"{field} must have {PARAM_COUNT} entries"):
        encode_preset(**{field: [0, 1, 2]})


@pytest.mark.parametrize("path", ["/etc/passwd", "clips/a.bin", "", "sdcard:/a"])
def test_path_prefix_required(shell, path):
    """Filesystem paths must be rooted at the SD card."""
    with pytest.raises(ValueError, match="must start with"):
        shell.stat(path)


@pytest.mark.parametrize("path", ["sd:/../etc", "sd:/clips/../../x"])
def test_path_traversal_rejected(shell, path):
    """Parent-directory segments are refused outright."""
    with pytest.raises(ValueError, match="must not contain"):
        shell.stat(path)


def test_rename_validates_both_paths(shell):
    """Both sides of a rename are checked."""
    with pytest.raises(ValueError, match="must start with"):
        shell.rename("sd:/a", "b")


@pytest.mark.parametrize("index", [-1, PARAM_COUNT, 99])
def test_modulation_index_validation(shell, index):
    """Modulator indices are zero-based and bounded."""
    with pytest.raises(ValueError, match="modulator index must be"):
        shell.set_modulation(index, 0)
    with pytest.raises(ValueError, match="modulator index must be"):
        shell.set_source(index, 0)


@pytest.mark.parametrize(
    "args,expected",
    [
        ((0, 512), "modulation set 0 512"),
        ((0, 512, 100), "modulation set 0 512 100"),
        ((0, 512, 100, 200), "modulation set 0 512 100 200"),
        ((0, 512, 100, 200, 300), "modulation set 0 512 100 200 300"),
        ((0, 512, None, 200, 300), "modulation set 0 512"),
        ((0, 512, 100, None, 300), "modulation set 0 512 100"),
        ((11, 0, 1, 2, None), "modulation set 11 0 1 2"),
    ],
)
def test_set_modulation_optional_args(shell, byte_transport, args, expected):
    """Optional knob values append in order and stop at the first omission."""
    shell.set_modulation(*args)
    assert byte_transport.last == expected


@pytest.mark.parametrize("source,code", [("free lfo", 1), ("FREE_LFO", 1), (12, 12), ("disabled", 0)])
def test_set_source_resolves_operator_names(shell, byte_transport, source, code):
    """Operator names and ids both reach the wire as ids."""
    shell.set_source(4, source)
    assert byte_transport.last == f"modulation source 4 {code}"


def test_programs_follows_pagination(shell, byte_transport):
    """Paginated listings are concatenated until ``more`` is false."""
    byte_transport.on("programs list", '{"programs":["a","b"],"more":true,"next":2}')
    byte_transport.on("programs list 2", '{"programs":["c"],"more":false}')
    assert shell.programs() == ["a", "b", "c"]


def test_programs_stops_on_non_advancing_cursor(shell, byte_transport):
    """A cursor that does not advance ends the walk instead of looping."""
    byte_transport.on("programs list", '{"programs":["a"],"more":true,"next":0}')
    assert shell.programs() == ["a"]


def test_programs_stops_on_missing_cursor(shell, byte_transport):
    """``more`` without ``next`` also ends the walk."""
    byte_transport.on("programs list", '{"programs":["a"],"more":true}')
    assert shell.programs() == ["a"]


def test_load_program_without_settle(shell, byte_transport):
    """Loading with no settle delay still issues the command."""
    assert shell.load_program("perlin", settle=0) is shell
    assert byte_transport.last == "program load perlin"


def test_load_program_settles(shell, byte_transport, monkeypatch):
    """The default settle delay is applied after the load command."""
    slept = []
    monkeypatch.setattr("pyvmancer.shell.time.sleep", slept.append)
    shell.load_program("moire")
    assert byte_transport.last == "program load moire"
    assert slept == [1.5]


def test_transport_status_decodes_state_and_bpm(shell, byte_transport):
    """State becomes an enum and hundredths of a BPM become a float."""
    byte_transport.on("transport status", '{"state":"playing","bpm_x100":12000}')
    data = shell.transport_status()
    assert data["state"] is TransportState.PLAYING
    assert data["bpm"] == pytest.approx(120.0)


def test_transport_status_passes_unknown_shape_through(shell, byte_transport):
    """Fields the firmware omits are simply absent."""
    byte_transport.on("transport status", '{"fields":7}')
    assert shell.transport_status() == {"fields": 7}


@pytest.mark.parametrize("bpm", [9.9, 400.1, 0, -1])
def test_set_bpm_range(shell, bpm):
    """Tempo is bounded by the firmware."""
    with pytest.raises(ValueError, match="bpm must be"):
        shell.set_bpm(bpm)


def test_midi_monitor_mode_validation(shell):
    """Only the three documented monitor modes are accepted."""
    with pytest.raises(ValueError, match="mode must be"):
        shell.midi_monitor("loud")


def _script_file(byte_transport, path, data, limit):
    """Script stat/caps/read replies for a whole-file read."""
    byte_transport.on("fs caps", json.dumps({"read_max_bytes": limit}))
    byte_transport.on(f"fs stat {path}", json.dumps({"size": len(data)}))
    for offset in range(0, len(data), limit):
        count = min(limit, len(data) - offset)
        chunk = base64.b64encode(data[offset : offset + count]).decode("ascii")
        byte_transport.on(f"fs read {path} {offset} {count}", chunk)


def test_read_file_chunks_with_base64(shell, byte_transport):
    """A file larger than one chunk is fetched in offset/length pages."""
    data = bytes(range(256)) + bytes(range(44))
    _script_file(byte_transport, "sd:/a.bin", data, 256)
    assert shell.read_file("sd:/a.bin") == data
    assert byte_transport.written[-2:] == ["fs read sd:/a.bin 0 256", "fs read sd:/a.bin 256 44"]


def test_read_file_honours_explicit_chunk(shell, byte_transport):
    """An explicit chunk size skips the capability query."""
    data = bytes(range(32))
    _script_file(byte_transport, "sd:/b.bin", data, 16)
    assert shell.read_file("sd:/b.bin", chunk=16) == data
    assert "fs caps" not in byte_transport.written


def test_read_file_stops_on_empty_payload(shell, byte_transport):
    """A short device reply ends the transfer rather than spinning."""
    byte_transport.on("fs caps", '{"read_max_bytes":16}')
    byte_transport.on("fs stat sd:/c.bin", '{"size":64}')
    byte_transport.on("fs read sd:/c.bin 0 16", "")
    assert shell.read_file("sd:/c.bin") == b""


def test_read_file_empty_file(shell, byte_transport):
    """A zero-length file needs no read commands."""
    byte_transport.on("fs caps", '{"read_max_bytes":16}')
    byte_transport.on("fs stat sd:/d.bin", '{"size":0}')
    assert shell.read_file("sd:/d.bin") == b""


def test_read_limit_falls_back_when_caps_fail(shell, byte_transport):
    """An unsupported ``fs caps`` falls back to the conservative default."""
    byte_transport.on_error("fs caps", 1, "unknown command")
    assert shell._read_limit() == READ_CHUNK


def test_read_limit_falls_back_on_bad_value(shell, byte_transport):
    """A non-numeric limit falls back too."""
    byte_transport.on("fs caps", '{"read_max_bytes":"lots"}')
    assert shell._read_limit() == READ_CHUNK


def test_write_file_chunks_with_base64(shell, byte_transport):
    """Writes are base64 chunks addressed by byte offset."""
    data = bytes(range(20))
    assert shell.write_file("sd:/w.bin", data, chunk=8) is shell
    expected = [
        f"fs write sd:/w.bin {offset} " + base64.b64encode(data[offset : offset + 8]).decode("ascii")
        for offset in (0, 8, 16)
    ]
    assert byte_transport.written == expected


def test_write_file_empty(shell, byte_transport):
    """Writing nothing issues no commands."""
    shell.write_file("sd:/w.bin", b"")
    assert byte_transport.written == []


def test_close_and_context_manager():
    """Leaving the context closes the transport."""
    transport = FakeByteTransport()
    with ShellClient(transport) as client:
        assert client.transport is transport
    assert transport.closed
    assert transport.name == "FakeByteTransport"


def test_byte_transport_is_its_own_context_manager():
    """The ByteTransport base class closes itself on exit."""
    transport = FakeByteTransport()
    with transport as entered:
        assert entered is transport
    assert transport.closed
