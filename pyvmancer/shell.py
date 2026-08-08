"""Videomancer serial shell protocol.

Commands are newline-terminated ASCII. Replies are ``@<command>:<payload>`` on
success and ``!<code>:<message>`` on failure; unprefixed lines are log output.
The command set is read from the device with :meth:`ShellClient.help`.
"""

import base64
import json
import time

from . import scaling
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
from .programs import MANIFEST_PATH, ProgramManifest
from .video import VideoStatus, parse_timings

OK = "ok"
#: Firmware tags shell error codes with this bit pattern; the low half is the code.
ERROR_TAG_MASK = 0x58000000
#: ``fs caps`` ``read_max_bytes`` default; it bounds the base64 payload, not the bytes.
READ_CHUNK = 256
WRITE_CHUNK = 256
#: Combined ``Manual + Modulation + MIDI`` output keys, in order of preference.
COMBINED_KEYS = ("o", "out", "output", "combined")
#: Stored manual value keys.
MANUAL_KEYS = ("m", "manual")
#: Default seconds to wait after a timing change before re-reading the lock flags.
RESYNC_SETTLE = 3.5
#: ``fs hash`` throughput measured on hardware, bytes per second.
HASH_BYTES_PER_SECOND = 79_000
#: Headroom over the measured hash rate, for a slower card or a busier device.
HASH_TIMEOUT_FACTOR = 4.0
#: Bytes per write while streaming an ``fs put`` payload.
PUT_CHUNK = 4096
#: ``fs put`` throughput measured on hardware, bytes per second.
PUT_BYTES_PER_SECOND = 157_000
#: Headroom over the measured put rate, for a slower card or a busier device.
PUT_TIMEOUT_FACTOR = 4.0
#: ``fs caps`` ``put_max_bytes`` default, bounding one ``fs put`` payload.
PUT_MAX_BYTES = 8 << 20
#: Entries one ``fs ls`` page returns.
LS_PAGE = 5


def decoded_limit(payload_max):
    """Decoded bytes that fit in a base64 payload of ``payload_max`` characters."""
    return max(3, int(payload_max) // 4 * 3)


def _pick(data, keys):
    """First present key of ``keys`` in ``data``."""
    return next((data[key] for key in keys if key in data), None)


def _slot_vector(payload, keys):
    """Extract a 12-slot value vector from parallel arrays or per-slot dicts."""
    if not isinstance(payload, dict):
        return None
    for value in [payload.get(key) for key in keys] + list(payload.values()):
        if not isinstance(value, (list, tuple)) or len(value) != PARAM_COUNT:
            continue
        if all(isinstance(slot, dict) for slot in value):
            slots = [_pick(slot, keys) for slot in value]
            if all(slot is not None for slot in slots):
                return scaling.clamp(slots)
        elif all(isinstance(slot, (int, float)) and not isinstance(slot, bool) for slot in value):
            return scaling.clamp(value)
    return None


def written_count(reply, expected, command):
    """Byte count acknowledged by a write, whichever form the firmware uses.

    ``1.0.0-rc.37`` and ``rc.40`` answer ``fs write`` with a bare ``ok``;
    ``rc.46`` answers with ``{"written": n}``, and ``fs put`` answers
    ``{"put": "ok", "written": n}``. A short count is a failed write.
    """
    if reply.ok:
        return expected
    try:
        payload = reply.json()
    except VmancerError as err:
        raise VmancerError(f"{command}: expected ok or a written count, got {reply.payload!r}") from err
    written = payload.get("written")
    if written is None:
        raise VmancerError(f"{command}: reply carries no written count: {reply.payload!r}")
    if int(written) != expected:
        raise VmancerError(f"{command}: device wrote {written} of {expected} bytes")
    return int(written)


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
        """Read lines until a reply or error arrives, collecting log output.

        Lines are taken one at a time rather than drained in a batch, so
        whatever follows a reply in the same read stays buffered for the next
        call instead of being dropped with the batch.
        """
        deadline = time.monotonic() + timeout
        while True:
            line = self._pop_line()
            while line is not None:
                kind, value = parse_line(line)
                if kind == "reply":
                    return value
                if kind == "error":
                    raise ShellError(value.code, value.message, command)
                if value:
                    self.log_lines.append(value)
                line = self._pop_line()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ShellTimeoutError(command, timeout)
            chunk = self._transport.read(timeout=min(0.2, remaining))
            if chunk:
                self._buffer.extend(chunk)

    def _pop_line(self):
        """Pop one complete newline-delimited line, or ``None`` when none is buffered."""
        index = self._buffer.find(b"\n")
        if index < 0:
            return None
        raw = bytes(self._buffer[:index])
        del self._buffer[: index + 1]
        return raw.decode("ascii", "replace")

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

    def video_state(self):
        """``video status`` parsed into a :class:`~pyvmancer.video.VideoStatus`."""
        return VideoStatus.from_json(self.video_status())

    def set_video_input(self, source):
        """Select the video input source."""
        return self._ok("video", "input", source)

    def set_video_timing(self, timing):
        """Force a video timing standard, e.g. ``NTSC`` or ``1080p30``."""
        return self._ok("video", "timing", timing)

    def video_timings(self):
        """Timing standards this firmware accepts, read from its usage string."""
        try:
            return parse_timings(self.command("video", "timing").payload)
        except ShellError as err:
            return parse_timings(err.message)

    def resync(self, alternate=None, settle=RESYNC_SETTLE, attempts=4, sleep=time.sleep):
        """Re-initialise the output raster by bouncing the video timing.

        Bounces through a standard the source cannot satisfy and back; ``alternate``
        defaults to the first accepted standard that differs. The bounce drops the
        input selection, so it is reasserted, and leaves ``overridden: true``.
        """
        status = self.video_state()
        if not status.timing:
            return False
        alternate = alternate or next((t for t in self.video_timings() if t != status.timing), None)
        if not alternate:
            return False
        self.set_video_timing(alternate)
        sleep(settle)
        self.set_video_timing(status.timing)
        sleep(settle)
        if status.input_source:
            self.set_video_input(status.input_source)
        for _ in range(max(1, attempts)):
            sleep(settle)
            if self.video_state().source_locked:
                return True
        return False

    def combined_values(self):
        """Combined ``Manual + Modulation + MIDI`` value of all 12 slots.

        Read from ``modulation status``, falling back to the stored manual values
        in ``program state`` on firmware that omits the combined output.
        """
        for read, keys in ((self.modulation_status, COMBINED_KEYS), (self.program_state, MANUAL_KEYS)):
            values = _slot_vector(read(), keys)
            if values is not None:
                return values
        raise VmancerError("device reported no 12-slot parameter state")

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
        """Read a whole file, following the paginated base64 chunk protocol.

        ``fs read`` answers ``{"data": "<base64>", "read": n}``; ``chunk`` counts
        decoded bytes, which the firmware limit bounds only after encoding.
        """
        target = _check_path(path)
        limit = chunk or self._read_limit()
        size = int(self.stat(target).get("size", 0))
        out = bytearray()
        while len(out) < size:
            reply = self.command("fs", "read", target, len(out), min(limit, size - len(out)))
            data = base64.b64decode(reply.json().get("data", "")) if reply.payload else b""
            if not data:
                break
            out.extend(data)
        return bytes(out)

    def _read_limit(self):
        """Decoded bytes per ``fs read`` that stay inside the firmware's payload cap."""
        try:
            return decoded_limit(self.fs_caps().get("read_max_bytes", READ_CHUNK))
        except (VmancerError, ValueError, TypeError):
            return decoded_limit(READ_CHUNK)

    def hash_timeout(self, size):
        """Deadline for hashing ``size`` bytes, from the measured device throughput."""
        return max(self.timeout, HASH_TIMEOUT_FACTOR * int(size) / HASH_BYTES_PER_SECOND)

    def hash_file(self, path, timeout=None):
        """Device-computed ``{"hash": <sha256 hex>, "size": n}`` for a file.

        Advertised as ``fs_hash`` in ``fs caps``; the digest is the natural cache
        key for anything derived from a program binary. ``timeout`` defaults to a
        deadline derived from the file's size, since hashing is far from instant.
        """
        target = _check_path(path)
        if timeout is None:
            timeout = self.hash_timeout(self.stat(target).get("size", 0))
        return self.command("fs", "hash", target, timeout=timeout).json()

    def program_manifest(self, path=MANIFEST_PATH):
        """Parsed program library manifest, covering SD-installed programs only."""
        return ProgramManifest.from_bytes(self.read_file(path))

    def write_file(self, path, data, chunk=WRITE_CHUNK):
        """Write bytes to a file in base64 chunks.

        The chunk is bounded so the rendered command, encoded payload included,
        stays inside the shell's line limit however long the path is. That caps
        the payload near 96 bytes per command, so this is for small files;
        :meth:`put_file` is the path for anything program-sized.
        """
        target = _check_path(path)
        payload = bytes(data)
        overhead = len(f"fs write {target} {len(payload)} ")
        limit = min(chunk, decoded_limit(SHELL_MAX_LINE - overhead))
        for offset in range(0, len(payload), limit):
            part = payload[offset : offset + limit]
            encoded = base64.b64encode(part).decode("ascii")
            reply = self.command("fs", "write", target, offset, encoded)
            written_count(reply, len(part), f"fs write {target}")
        return self

    def put_timeout(self, size):
        """Deadline for streaming ``size`` bytes, from the measured device throughput."""
        return max(self.timeout, PUT_TIMEOUT_FACTOR * int(size) / PUT_BYTES_PER_SECOND)

    def put_max_bytes(self):
        """Largest payload one ``fs put`` accepts, as ``fs caps`` reports it."""
        try:
            return int(self.fs_caps().get("put_max_bytes", PUT_MAX_BYTES))
        except (VmancerError, ValueError, TypeError):
            return PUT_MAX_BYTES

    def put_file(self, path, data, chunk=PUT_CHUNK, timeout=None):
        """Upload a whole file with ``fs put``, the raw streaming path.

        ``fs put <path> <size>`` answers ``{"put": "ready"}`` and the device then
        takes exactly ``size`` bytes off the link as the payload, with no
        encoding and no per-chunk commands. Measured at about 157 kB/s against
        roughly 3 kB/s for the base64 ``fs write`` path, which is the difference
        between minutes and days for a program library.

        Once the handshake is answered the payload owns the link until its last
        byte: the device takes anything else sent as file content, and a client
        that abandons the transfer strands it consuming commands as payload,
        recoverable only by feeding the promised byte count or power cycling.
        Abandoning it is therefore what has to be avoided. The device stalls
        while it commits to the card -- past two seconds when a program-sized
        payload lands in a directory already holding the library -- so the
        transports allow :data:`~pyvmancer.transports.serial_tty.WRITE_TIMEOUT`
        for a blocked write rather than treating a stall as failure.
        """
        target = _check_path(path)
        payload = bytes(data)
        limit = self.put_max_bytes()
        if len(payload) > limit:
            raise ValueError(f"payload of {len(payload)} bytes exceeds the device limit of {limit}")
        deadline = self.put_timeout(len(payload)) if timeout is None else timeout
        state = self.command("fs", "put", target, len(payload), timeout=deadline).json()
        if state.get("put") != "ready":
            raise VmancerError(f"fs put {target}: device did not report ready: {state!r}")
        for offset in range(0, len(payload), chunk):
            self._transport.write(payload[offset : offset + chunk])
        reply = self._await_reply(f"fs put {target}", deadline)
        written_count(reply, len(payload), f"fs put {target}")
        return self

    def listdir(self, path=SHELL_PATH_PREFIX):
        """Every entry in a directory, following ``fs ls`` pagination.

        The firmware answers a full page as ``{"entries": [...], "more": true,
        "next": n}`` and the final short page as a bare array whose last entry is
        repeated, so entries are de-duplicated by name.
        """
        target = _check_path(path)
        entries = {}
        offset = 0
        while True:
            payload = self.command("fs", "ls", target, offset or None).json()
            page = payload.get("entries", []) if isinstance(payload, dict) else payload
            for entry in page:
                entries.setdefault(entry.get("name"), entry)
            if not isinstance(payload, dict) or not payload.get("more"):
                return list(entries.values())
            nxt = payload.get("next")
            if nxt is None or nxt <= offset:
                return list(entries.values())
            offset = nxt

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
