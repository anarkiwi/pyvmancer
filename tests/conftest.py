"""In-memory fakes and fixtures. No test in this suite touches real hardware."""

import base64
import json

import pytest

from pyvmancer.midi import MidiController
from pyvmancer.shell import ShellClient, decoded_limit
from pyvmancer.transports.base import ByteTransport, MidiTransport


def reply_line(tag, payload):
    """Render an ``@tag:payload`` success line."""
    return f"@{tag}:{payload}"


def script_file(byte_transport, path, data, payload_max=256, chunk=None):
    """Script the caps/stat/read replies that satisfy one whole-file read."""
    byte_transport.on("fs caps", json.dumps({"read_max_bytes": payload_max}))
    byte_transport.on(f"fs stat {path}", json.dumps({"size": len(data)}))
    limit = chunk or decoded_limit(payload_max)
    for offset in range(0, len(data), limit):
        count = min(limit, len(data) - offset)
        encoded = base64.b64encode(data[offset : offset + count]).decode("ascii")
        byte_transport.on(f"fs read {path} {offset} {count}", json.dumps({"data": encoded, "read": count}))
    return byte_transport


def error_line(code, message):
    """Render a ``!code:message`` failure line."""
    return f"!{code}:{message}"


class FakeMidiTransport(MidiTransport):
    """MidiTransport that records sent bytes and replays primed input."""

    def __init__(self, incoming=b""):
        self.sent = []
        self.incoming = bytearray(incoming)
        self.timeouts = []
        self.closed = False

    def send(self, data):
        """Record outgoing bytes."""
        self.sent.append(bytes(data))

    def receive(self, timeout=0.0):
        """Return and clear the primed input buffer."""
        self.timeouts.append(timeout)
        data = bytes(self.incoming)
        self.incoming.clear()
        return data

    def close(self):
        """Mark the transport closed."""
        self.closed = True

    def prime(self, data):
        """Queue bytes for the next ``receive``."""
        self.incoming.extend(data)
        return self

    @property
    def data(self):
        """Every byte sent so far, concatenated."""
        return b"".join(self.sent)


class FakeByteTransport(ByteTransport):
    """ByteTransport driven by a per-command script of reply lines.

    ``default_payload`` answers unscripted commands with ``@<first word>:<payload>``;
    set it to ``None`` to make unscripted commands hang (for timeout tests).
    """

    def __init__(self, default_payload="ok"):
        self.default_payload = default_payload
        self.written = []
        self.raw = bytearray()
        self.scripts = {}
        self.closed = False
        self._pending = bytearray()

    def on(self, command, payload, tag=None):
        """Script an ``@tag:payload`` reply for an exact command line."""
        self.scripts[command] = [reply_line(tag or command.split(" ")[0], payload)]
        return self

    def on_error(self, command, code, message):
        """Script a ``!code:message`` failure for an exact command line."""
        self.scripts[command] = [error_line(code, message)]
        return self

    def on_lines(self, command, lines):
        """Script arbitrary raw lines (logs, then a reply) for a command line."""
        self.scripts[command] = list(lines)
        return self

    def silence(self, command):
        """Script no response at all for a command line."""
        self.scripts[command] = []
        return self

    def write(self, data):
        """Split incoming bytes into command lines and queue their scripted replies."""
        text = bytes(data).decode("ascii")
        self.raw.extend(data)
        for line in text.split("\n")[:-1]:
            self.written.append(line)
            self._pending.extend(self._response(line))

    def _response(self, command):
        """Return the encoded reply lines for one command."""
        lines = self.scripts.get(command)
        if lines is None:
            if self.default_payload is None:
                return b""
            lines = [reply_line(command.split(" ")[0], self.default_payload)]
        return "".join(f"{line}\n" for line in lines).encode("ascii")

    def read(self, timeout=0.0):
        """Return and clear whatever replies are queued."""
        data = bytes(self._pending)
        self._pending.clear()
        return data

    def close(self):
        """Mark the transport closed."""
        self.closed = True

    @property
    def last(self):
        """The most recent command line written."""
        return self.written[-1]


@pytest.fixture(name="midi_transport")
def midi_transport_fixture():
    """A fresh FakeMidiTransport."""
    return FakeMidiTransport()


@pytest.fixture(name="midi")
def midi_fixture(midi_transport):
    """A high-resolution MidiController on a FakeMidiTransport."""
    return MidiController(midi_transport)


@pytest.fixture(name="byte_transport")
def byte_transport_fixture():
    """A fresh FakeByteTransport answering everything with ``ok``."""
    return FakeByteTransport()


@pytest.fixture(name="shell")
def shell_fixture(byte_transport):
    """A ShellClient on a FakeByteTransport with a short timeout."""
    return ShellClient(byte_transport, timeout=0.2)
