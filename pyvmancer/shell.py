"""Videomancer serial shell protocol.

Commands are newline-terminated ASCII. Replies are ``@<command>:<payload>`` on
success and ``!<code>:<message>`` on failure; unprefixed lines are log output.
The command set is read from the device with :meth:`ShellClient.help`.
"""

import base64
import json
import time

from .const import (
    BPM_MAX,
    BPM_MIN,
    BPM_SCALE,
    PARAM_COUNT,
    PRESET_FIELDS,
    SHELL_MAX_LINE,
    SHELL_PATH_PREFIX,
    LogLevel,
    PresetBank,
    TransportState,
    resolve_operator,
)
from .errors import ShellError, ShellTimeoutError, VmancerError

OK = "ok"
#: Firmware tags shell error codes with this bit pattern; the low half is the code.
ERROR_TAG_MASK = 0x58000000
#: ``fs caps`` reports ``read_max_bytes``; this is the conservative default.
READ_CHUNK = 256
WRITE_CHUNK = 256


def decode_error_code(code):
    """Strip the firmware's ``0x58000000`` tag from an error code."""
    if code is None:
        return None
    return code & 0xFFFF if code & ERROR_TAG_MASK == ERROR_TAG_MASK else code


class Reply:
    """One parsed ``@``-prefixed success reply."""

    __slots__ = ("tag", "payload")

    def __init__(self, tag, payload):
        self.tag = tag
        self.payload = payload

    @property
    def ok(self):
        """True when the payload is the bare ``ok`` acknowledgement."""
        return self.payload == OK

    def json(self):
        """Decode the payload as JSON."""
        try:
            return json.loads(self.payload)
        except json.JSONDecodeError as err:
            raise VmancerError(f"reply {self.tag!r} is not JSON: {self.payload!r}") from err

    def __repr__(self):
        return f"Reply(tag={self.tag!r}, payload={self.payload!r})"


def parse_line(line):
    """Classify one protocol line as ``("reply"|"error"|"log", value)``."""
    text = line.rstrip("\r\n")
    if text.startswith("@"):
        tag, _, payload = text[1:].partition(":")
        return "reply", Reply(tag, payload)
    if text.startswith("!"):
        code, _, message = text[1:].partition(":")
        try:
            code = decode_error_code(int(code))
        except ValueError:
            message = text[1:]
            code = None
        return "error", ShellError(code, message)
    return "log", text


def _arg(value):
    """Render a command argument, rejecting embedded newlines."""
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError(f"argument must not contain newlines: {text!r}")
    return text


def _check_path(path):
    """Validate an ``sd:/`` path."""
    text = str(path)
    if not text.startswith(SHELL_PATH_PREFIX):
        raise ValueError(f"path must start with {SHELL_PATH_PREFIX!r}, got {text!r}")
    if ".." in text:
        raise ValueError(f"path must not contain '..': {text!r}")
    return text


def _check_index(index):
    """Validate a zero-based modulator index."""
    index = int(index)
    if not 0 <= index < PARAM_COUNT:
        raise ValueError(f"modulator index must be 0..{PARAM_COUNT - 1}, got {index}")
    return index


class ShellClient:
    """Typed client for the Videomancer serial command set.

    The device shell is single-threaded and expects one connected terminal, so
    calls are strictly request/response.
    """

    def __init__(self, transport, timeout=5.0):
        self._transport = transport
        self.timeout = timeout
        self._buffer = bytearray()
        self.log_lines = []

    @property
    def transport(self):
        """The underlying byte transport."""
        return self._transport

    def command(self, *parts, timeout=None):
        """Send a command and return its :class:`Reply`."""
        command = " ".join(_arg(p) for p in parts if p is not None and p != "")
        if len(command) > SHELL_MAX_LINE:
            raise ValueError(f"command exceeds {SHELL_MAX_LINE} bytes: {len(command)}")
        self._buffer.clear()
        self._transport.write(command.encode("ascii", "strict") + b"\n")
        return self._await_reply(command, self.timeout if timeout is None else timeout)

    def _await_reply(self, command, timeout):
        """Read lines until a reply or error arrives, collecting log output."""
        deadline = time.monotonic() + timeout
        while True:
            for line in self._drain_lines():
                kind, value = parse_line(line)
                if kind == "reply":
                    return value
                if kind == "error":
                    raise ShellError(value.code, value.message, command)
                if value:
                    self.log_lines.append(value)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ShellTimeoutError(command, timeout)
            chunk = self._transport.read(timeout=min(0.2, remaining))
            if chunk:
                self._buffer.extend(chunk)

    def _drain_lines(self):
        """Pop complete newline-delimited lines out of the read buffer."""
        lines = []
        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                return lines
            raw = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            lines.append(raw.decode("ascii", "replace"))

    def _ok(self, *parts, accept=()):
        """Run a command and assert it acknowledged.

        Most commands reply ``ok``; a few use a verb of their own, which callers
        pass via ``accept``.
        """
        reply = self.command(*parts)
        if not reply.ok and reply.payload not in accept:
            expected = "/".join(("ok",) + tuple(accept))
            raise VmancerError(
                f"expected {expected} from {' '.join(map(str, parts))!r}, got {reply.payload!r}"
            )
        return self

    def version(self):
        """Firmware version string."""
        return self.command("version").payload

    def serial_number(self):
        """Unique hardware id."""
        return self.command("serial").payload

    def status(self):
        """System status: product, firmware, build, program count, current program."""
        return self.command("status").json()

    def help(self):
        """List of command strings this firmware supports."""
        return self.command("help").payload.split("|")

    def supports(self, command):
        """True when ``help`` advertises ``command``."""
        return command in self.help()

    def ram(self):
        """Memory usage snapshot."""
        return self.command("ram").json()

    def cpu(self):
        """Per-core CPU usage."""
        return self.command("cpu").json()

    def log_level(self, level):
        """Set diagnostic log verbosity; the device echoes the level it applied."""
        value = LogLevel(level).value
        return self._ok("log", "level", value, accept=(value, value.upper()))

    def language(self, locale=None):
        """Get or set the UI locale."""
        if locale is None:
            return self.command("language").payload
        return self._ok("language", locale)

    def reboot_bootloader(self):
        """Reboot into firmware update mode; the link drops, so no reply is awaited."""
        self._transport.write(b"reboot bootloader\n")
        return self

    def programs(self):
        """Every installed FPGA program name, following pagination if present."""
        names = []
        offset = 0
        while True:
            page = self.command("programs", "list", offset or None).json()
            names.extend(page.get("programs", []))
            if not page.get("more"):
                return names
            nxt = page.get("next")
            if nxt is None or nxt <= offset:
                return names
            offset = nxt

    def load_program(self, name, settle=1.5):
        """Load an FPGA program by name and wait for the load to settle."""
        self._ok("program", "load", name)
        if settle:
            time.sleep(settle)
        return self

    def program_info(self):
        """Current program's name, id and its 12 parameter names with min/max."""
        return self.command("program", "info").json()

    def program_state(self):
        """Modulation arrays ``m``, ``t``, ``sp``, ``sl``, ``sr`` for all 12 slots."""
        return self.command("program", "state").json()

    def modulation_status(self):
        """Per-modulator source, manual value and combined output."""
        return self.command("modulation", "status").json()

    def modulation_audio_status(self):
        """Audio/CV sampling and per-line rendering diagnostics."""
        return self.command("modulation", "audio", "status").json()

    def cc_map(self):
        """Per-modulator MIDI CC assignments as MSB/LSB pairs."""
        return self.command("modulation", "cc-map").json()

    def set_modulation(self, index, manual, time_=None, space=None, slope=None):
        """Set a modulator's manual value, and optionally its three mod knobs.

        ``index`` is zero-based. The time/space/slope knobs only take effect
        while that slot has an active (non-disabled) source.
        """
        parts = ["modulation", "set", _check_index(index), int(manual)]
        for value in (time_, space, slope):
            if value is None:
                break
            parts.append(int(value))
        return self._ok(*parts)

    def set_source(self, index, source):
        """Assign a modulation operator to a slot; ``source`` may be a name or id."""
        return self._ok("modulation", "source", _check_index(index), int(resolve_operator(source)))

    def reset_modulation(self):
        """Reset all modulation slots."""
        return self._ok("modulation", "reset")

    def latch_modulation(self):
        """Latch current modulation output into the manual values."""
        return self._ok("modulation", "latch")

    def presets(self):
        """Factory and user preset listing plus free flash bytes."""
        return self.command("program", "presets", "list").json()

    def get_preset(self, index, bank=PresetBank.FACTORY):
        """Fetch one preset's name and modulation arrays."""
        return self.command("program", "presets", "get", int(index), PresetBank(bank).value).json()

    def apply_preset(self, index, bank=None):
        """Apply a preset by ``(index, bank)`` or by name when ``bank`` is None."""
        if bank is None:
            return self._ok("program", "presets", "apply", index)
        return self._ok("program", "presets", "apply", int(index), PresetBank(bank).value)

    def save_preset(self, index, name, **fields):
        """Save a user preset, optionally overriding modulation fields.

        Accepts ``manual``, ``time``, ``space``, ``slope`` and ``source``
        keyword arguments, each a 12-element sequence.
        """
        return self._ok("program", "presets", "save", int(index), name, encode_preset(**fields))

    def delete_preset(self, index):
        """Delete a user preset and return the updated listing."""
        return self.command("program", "presets", "delete", int(index)).json()

    def rename_preset(self, index, name):
        """Rename a user preset."""
        return self._ok("program", "presets", "rename", int(index), name)

    def video_status(self):
        """Input/output video source, timing and lock state."""
        return self.command("video", "status").json()

    def set_video_input(self, source):
        """Select the video input source."""
        return self._ok("video", "input", source)

    def set_video_timing(self, timing):
        """Force a video timing standard, e.g. ``NTSC`` or ``1080p30``."""
        return self._ok("video", "timing", timing)

    def fpga_status(self):
        """FPGA configuration state and loaded program."""
        return self.command("fpga", "status").json()

    def fpga_register(self, addr, value):
        """Write a raw FPGA register."""
        return self._ok("fpga", "register", int(addr), int(value))

    def remote_status(self):
        """Remote-control mode, ``local`` or ``remote``."""
        return self.command("remote", "status").json()

    def remote_enable(self):
        """Take remote control of the panel."""
        return self._ok("remote", "enable", accept=("enabled",))

    def remote_disable(self):
        """Return control to the front panel."""
        return self._ok("remote", "disable", accept=("disabled",))

    def remote_write(self, *args):
        """Send a raw remote-control write."""
        return self._ok("remote", "write", *args)

    def get_setting(self, key_id):
        """Read one setting by numeric key id."""
        return self.command("settings", "get", int(key_id)).payload

    def set_setting(self, key_id, value):
        """Write one setting by numeric key id."""
        return self._ok("settings", "set", int(key_id), value)

    def reset_settings(self):
        """Restore factory settings."""
        return self._ok("settings", "reset")

    def import_settings(self, settings):
        """Atomically import a settings dict."""
        return self._ok("settings", "import", json.dumps(settings, separators=(",", ":")))

    def transport_status(self):
        """Transport state, BPM and field counter."""
        data = self.command("transport", "status").json()
        if "state" in data:
            data["state"] = TransportState(data["state"])
        if "bpm_x100" in data:
            data["bpm"] = data["bpm_x100"] / BPM_SCALE
        return data

    def set_bpm(self, bpm):
        """Set internal tempo in BPM."""
        if not BPM_MIN <= bpm <= BPM_MAX:
            raise ValueError(f"bpm must be {BPM_MIN}..{BPM_MAX}, got {bpm}")
        return self._ok("transport", "bpm", int(round(bpm * BPM_SCALE)))

    def play(self):
        """Start transport playback."""
        return self._ok("transport", "play")

    def stop(self):
        """Stop transport playback."""
        return self._ok("transport", "stop")

    def midi_monitor(self, mode="on"):
        """Enable, disable or verbosely enable MIDI input monitoring."""
        if mode not in ("on", "off", "verbose"):
            raise ValueError(f"mode must be on/off/verbose, got {mode!r}")
        return self._ok("midi", "monitor", mode, accept=(f"monitor {mode}",))

    def msd_status(self):
        """Whether the microSD is exported as USB mass storage."""
        return self.command("msd", "status").json()

    def msd_enter(self):
        """Export the microSD card over USB mass storage."""
        return self._ok("msd", "enter")

    def msd_exit(self):
        """Stop exporting the microSD card."""
        return self._ok("msd", "exit")

    def fs_info(self):
        """microSD mount status and capacity."""
        return self.command("fs", "info").json()

    def fs_caps(self):
        """Filesystem protocol capabilities and transfer limits."""
        return self.command("fs", "caps").json()

    def ls(self, path=SHELL_PATH_PREFIX):
        """List a directory."""
        return self.command("fs", "ls", _check_path(path)).json()

    def stat(self, path):
        """File or directory metadata."""
        return self.command("fs", "stat", _check_path(path)).json()

    def mkdir(self, path):
        """Create a directory, including parents."""
        return self._ok("fs", "mkdir", _check_path(path))

    def remove(self, path):
        """Remove a file or empty directory."""
        return self._ok("fs", "rm", _check_path(path))

    def rename(self, old, new):
        """Rename or move a file or directory."""
        return self._ok("fs", "rename", _check_path(old), _check_path(new))

    def read_file(self, path, chunk=None):
        """Read a whole file, following the paginated base64 chunk protocol."""
        target = _check_path(path)
        limit = chunk or self._read_limit()
        size = int(self.stat(target).get("size", 0))
        out = bytearray()
        while len(out) < size:
            payload = self.command("fs", "read", target, len(out), min(limit, size - len(out))).payload
            data = base64.b64decode(payload) if payload else b""
            if not data:
                break
            out.extend(data)
        return bytes(out)

    def _read_limit(self):
        """Largest ``fs read`` chunk this firmware accepts."""
        try:
            return int(self.fs_caps().get("read_max_bytes", READ_CHUNK))
        except (VmancerError, ValueError):
            return READ_CHUNK

    def write_file(self, path, data, chunk=WRITE_CHUNK):
        """Write bytes to a file in base64 chunks."""
        target = _check_path(path)
        payload = bytes(data)
        for offset in range(0, len(payload), chunk):
            encoded = base64.b64encode(payload[offset : offset + chunk]).decode("ascii")
            self._ok("fs", "write", target, offset, encoded)
        return self

    def close(self):
        """Close the underlying transport."""
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def encode_preset(**fields):
    """Encode preset modulation fields into the ``m:...`` wire format."""
    parts = []
    for key, prefix in PRESET_FIELDS.items():
        values = fields.get(key)
        if values is None:
            continue
        values = list(values)
        if len(values) != PARAM_COUNT:
            raise ValueError(f"{key} must have {PARAM_COUNT} entries, got {len(values)}")
        parts.append(f"{prefix}:" + ",".join(str(int(v)) for v in values))
    return " ".join(parts)
