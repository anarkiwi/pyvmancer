"""Transport backends driven by fakes: a local FIFO and injected third-party modules.

No test here opens a real device node under /dev/snd, /dev/ttyACM* or /dev/bus/usb.
Fakes mirror third-party signatures, so unused arguments and synthesised module
attributes are expected.
"""

# pylint: disable=unused-argument,no-member

import os
import sys
import types

import pytest

from pyvmancer.errors import DeviceNotFoundError, TransportError, TransportUnavailableError
from pyvmancer.transports import rawmidi, usb_cdc
from pyvmancer.transports.mido_port import MidoTransport
from pyvmancer.transports.rawmidi import RawMidiTransport
from pyvmancer.transports.serial_tty import SerialTransport
from pyvmancer.transports.usb_cdc import UsbCdcTransport

# pylint: disable=protected-access


@pytest.fixture(name="fifo")
def fifo_fixture(tmp_path):
    """A local FIFO standing in for an ALSA rawmidi character device."""
    path = tmp_path / "midiFAKE"
    os.mkfifo(path)
    return str(path)


@pytest.fixture(name="raw")
def raw_fixture(fifo):
    """An open RawMidiTransport on the FIFO."""
    transport = RawMidiTransport(fifo)
    yield transport
    if transport._fd is not None:
        transport.close()


def test_rawmidi_open_failure(tmp_path):
    """A missing node is an unavailable transport, not a crash."""
    with pytest.raises(TransportUnavailableError, match="cannot open"):
        RawMidiTransport(str(tmp_path / "absent"))


def test_rawmidi_name_is_the_path(raw, fifo):
    """The transport names itself after its node."""
    assert raw.name == fifo


def test_rawmidi_send_and_receive(raw):
    """Bytes written to the node read back out of it."""
    raw.send(bytes([0xF8, 0x90, 0x00, 0x7F]))
    assert raw.receive(timeout=0.5) == bytes([0xF8, 0x90, 0x00, 0x7F])


def test_rawmidi_receive_times_out_empty(raw):
    """With nothing buffered, receive returns no bytes."""
    assert raw.receive(timeout=0.01) == b""


def test_rawmidi_receive_reads_multiple_chunks(raw):
    """Payloads larger than one read are concatenated."""
    payload = bytes(range(256)) * 24
    raw.send(payload)
    assert raw.receive(timeout=0.5) == payload


def test_rawmidi_send_retries_on_full_buffer(raw, monkeypatch):
    """A full kernel buffer is waited on rather than treated as an error."""
    real_write = os.write
    calls = []

    def flaky_write(fd, data):
        """Raise once, then delegate."""
        calls.append(len(data))
        if len(calls) == 1:
            raise BlockingIOError(11, "would block")
        return real_write(fd, data)

    monkeypatch.setattr(rawmidi.os, "write", flaky_write)
    raw.send(b"\xf8\xfa")
    monkeypatch.undo()
    assert len(calls) == 2
    assert raw.receive(timeout=0.5) == b"\xf8\xfa"


def test_rawmidi_send_partial_writes(raw, monkeypatch):
    """A short write is resumed from where it stopped."""
    real_write = os.write

    def one_byte_write(fd, data):
        """Write a single byte at a time."""
        return real_write(fd, bytes(data)[:1])

    monkeypatch.setattr(rawmidi.os, "write", one_byte_write)
    raw.send(b"\xf8\xfa\xfc")
    monkeypatch.undo()
    assert raw.receive(timeout=0.5) == b"\xf8\xfa\xfc"


def test_rawmidi_send_error(raw, monkeypatch):
    """An OS write error surfaces as a TransportError."""

    def failing_write(fd, data):
        """Always fail."""
        raise OSError(5, "I/O error")

    monkeypatch.setattr(rawmidi.os, "write", failing_write)
    with pytest.raises(TransportError, match="write to .* failed"):
        raw.send(b"\xf8")


def test_rawmidi_receive_error(raw, monkeypatch):
    """An OS read error surfaces as a TransportError."""
    raw.send(b"\xf8")

    def failing_read(fd, size):
        """Always fail."""
        raise OSError(5, "I/O error")

    monkeypatch.setattr(rawmidi.os, "read", failing_read)
    with pytest.raises(TransportError, match="read from .* failed"):
        raw.receive(timeout=0.5)


def test_rawmidi_receive_stops_at_end_of_stream(raw, monkeypatch):
    """An empty read ends the drain loop."""
    raw.send(b"\xf8")
    monkeypatch.setattr(rawmidi.select, "select", lambda *args: ([raw._fd], [], []))
    monkeypatch.setattr(rawmidi.os, "read", lambda fd, size: b"")
    assert raw.receive(timeout=0.0) == b""


def test_rawmidi_receive_stops_when_the_buffer_drains(raw, monkeypatch):
    """A would-block read after a full chunk ends the drain loop."""
    chunks = [bytes(4096)]

    def draining_read(fd, size):
        """Return one full chunk, then signal that nothing more is buffered."""
        if chunks:
            return chunks.pop()
        raise BlockingIOError(11, "would block")

    raw.send(b"\xf8")
    monkeypatch.setattr(rawmidi.os, "read", draining_read)
    assert raw.receive(timeout=0.5) == bytes(4096)


def test_rawmidi_operations_after_close(raw):
    """A closed transport refuses further traffic and closes idempotently."""
    raw.close()
    raw.close()
    with pytest.raises(TransportError, match="closed"):
        raw.send(b"\xf8")
    with pytest.raises(TransportError, match="closed"):
        raw.receive()


def test_rawmidi_context_manager(fifo):
    """The base-class context manager closes the node."""
    with RawMidiTransport(fifo) as transport:
        transport.send(b"\xf8")
    assert transport._fd is None


class _FakeSerialPort:
    """Minimal pyserial ``Serial`` stand-in backed by an in-memory buffer."""

    def __init__(self, port, baudrate=115200, timeout=0, write_timeout=None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.written = bytearray()
        self.buffer = bytearray()
        self.closed = False
        self.fail = None

    @property
    def in_waiting(self):
        """Bytes still queued for reading."""
        return len(self.buffer)

    def write(self, data):
        """Record outgoing bytes."""
        if self.fail:
            raise OSError(self.fail)
        self.written.extend(data)
        return len(data)

    def flush(self):
        """No-op flush."""

    def read(self, size=1):
        """Pop up to ``size`` buffered bytes."""
        if self.fail:
            raise OSError(self.fail)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def close(self):
        """Mark the port closed."""
        self.closed = True


def _serial_module(port_factory=_FakeSerialPort):
    """Build a fake ``serial`` module exposing ``Serial``."""
    module = types.ModuleType("serial")
    module.Serial = port_factory
    return module


@pytest.fixture(name="serial_port")
def serial_port_fixture(monkeypatch):
    """Install a fake ``serial`` module and hand back the last port it built."""
    created = []

    def factory(*args, **kwargs):
        """Record every port constructed."""
        port = _FakeSerialPort(*args, **kwargs)
        created.append(port)
        return port

    monkeypatch.setitem(sys.modules, "serial", _serial_module(factory))
    return created


def test_serial_transport_write_and_read(serial_port):
    """Bytes round-trip through the fake port."""
    transport = SerialTransport("/dev/ttyFAKE")
    transport.write(b"version\n")
    port = serial_port[0]
    assert port.written == b"version\n"
    assert transport.name == "/dev/ttyFAKE"
    port.buffer.extend(b"@version:1.0\n")
    assert transport.read(timeout=0.1) == b"@version:1.0\n"
    assert port.timeout == pytest.approx(0.1)


def test_serial_transport_read_empty(serial_port):
    """No buffered data reads as no bytes."""
    transport = SerialTransport("/dev/ttyFAKE")
    assert transport.read() == b""
    assert serial_port


def test_serial_transport_missing_library(monkeypatch):
    """Without pyserial installed the backend is unavailable."""
    monkeypatch.setitem(sys.modules, "serial", None)
    with pytest.raises(TransportUnavailableError, match="pyserial is not installed"):
        SerialTransport("/dev/ttyFAKE")


def test_serial_transport_open_failure(monkeypatch):
    """A port that will not open is an unavailable transport."""

    def boom(*args, **kwargs):
        """Refuse to open."""
        raise OSError("no such port")

    monkeypatch.setitem(sys.modules, "serial", _serial_module(boom))
    with pytest.raises(TransportUnavailableError, match="cannot open"):
        SerialTransport("/dev/ttyFAKE")


def test_serial_transport_io_errors(serial_port):
    """Errors from the port become TransportErrors naming the port."""
    transport = SerialTransport("/dev/ttyFAKE")
    serial_port[0].fail = "broken"
    with pytest.raises(TransportError, match="write to .* failed"):
        transport.write(b"x")
    with pytest.raises(TransportError, match="read from .* failed"):
        transport.read()


def test_serial_transport_after_close(serial_port):
    """A closed port refuses further traffic."""
    transport = SerialTransport("/dev/ttyFAKE")
    transport.close()
    assert serial_port[0].closed
    transport.close()
    with pytest.raises(TransportError, match="closed"):
        transport.write(b"x")
    with pytest.raises(TransportError, match="closed"):
        transport.read()


class _FakeMidoPort:
    """Minimal mido port stand-in."""

    def __init__(self, name):
        self.name = name
        self.sent = []
        self.pending = []
        self.closed = False

    def send(self, message):
        """Record an outgoing message."""
        self.sent.append(message)

    def poll(self):
        """Pop one pending incoming message."""
        return self.pending.pop(0) if self.pending else None

    def close(self):
        """Mark the port closed."""
        self.closed = True


@pytest.fixture(name="mido_ports")
def mido_ports_fixture(monkeypatch):
    """Patch mido's port factories to return in-memory ports."""
    import mido

    ports = {}

    def open_output(name):
        """Create a fake output port."""
        ports["out"] = _FakeMidoPort(name)
        return ports["out"]

    def open_input(name):
        """Create a fake input port."""
        ports["in"] = _FakeMidoPort(name)
        return ports["in"]

    monkeypatch.setattr(mido, "open_output", open_output)
    monkeypatch.setattr(mido, "open_input", open_input)
    return ports


def test_mido_transport_send(mido_ports):
    """Raw bytes are parsed into messages before being sent."""
    transport = MidoTransport("Videomancer:0")
    transport.send(bytes([0xB0, 0x00, 0x40, 0xF8]))
    assert transport.name == "Videomancer:0"
    assert [m.type for m in mido_ports["out"].sent] == ["control_change", "clock"]


def test_mido_transport_receive(mido_ports):
    """Pending input messages are drained back into raw bytes."""
    import mido

    transport = MidoTransport("Videomancer:0", input_name="Videomancer:1")
    assert mido_ports["in"].name == "Videomancer:1"
    mido_ports["in"].pending.append(mido.Message("clock"))
    assert transport.receive(timeout=0.5) == b"\xf8"


def test_mido_transport_receive_timeout(mido_ports):
    """Nothing pending yields no bytes once the deadline passes."""
    transport = MidoTransport("Videomancer:0")
    assert transport.receive(timeout=0.01) == b""
    assert mido_ports


def test_mido_transport_without_input_port(monkeypatch, mido_ports):
    """An input port that will not open leaves the transport send-only."""
    import mido

    def boom(name):
        """Refuse to open an input."""
        raise OSError("no input")

    monkeypatch.setattr(mido, "open_input", boom)
    transport = MidoTransport("Videomancer:0")
    assert transport.receive(timeout=0.5) == b""
    assert "in" not in mido_ports
    transport.close()
    assert mido_ports["out"].closed


def test_mido_transport_output_failure(monkeypatch):
    """An output port that will not open is an unavailable transport."""
    import mido

    def boom(name):
        """Refuse to open an output."""
        raise OSError("busy")

    monkeypatch.setattr(mido, "open_output", boom)
    with pytest.raises(TransportUnavailableError, match="cannot open MIDI output"):
        MidoTransport("Videomancer:0")


def test_mido_transport_missing_library(monkeypatch):
    """Without mido installed the backend is unavailable."""
    monkeypatch.setitem(sys.modules, "mido", None)
    with pytest.raises(TransportUnavailableError, match="mido is not installed"):
        MidoTransport("Videomancer:0")


def test_mido_transport_send_error(mido_ports):
    """A failing port surfaces as a TransportError."""
    transport = MidoTransport("Videomancer:0")

    def boom(message):
        """Refuse to send."""
        raise OSError("gone")

    mido_ports["out"].send = boom
    with pytest.raises(TransportError, match="send on .* failed"):
        transport.send(b"\xf8")


def test_mido_transport_close(mido_ports):
    """Closing releases both ports and blocks further sends."""
    transport = MidoTransport("Videomancer:0")
    transport.close()
    assert mido_ports["out"].closed and mido_ports["in"].closed
    with pytest.raises(TransportError, match="closed"):
        transport.send(b"\xf8")


class _FakeEndpoint:
    """Bulk endpoint stand-in."""

    wMaxPacketSize = 64  # pylint: disable=invalid-name

    def __init__(self, address, buffer=None):
        self.bEndpointAddress = address  # pylint: disable=invalid-name
        self.written = bytearray()
        self.buffer = bytearray(buffer or b"")
        self.fail = None

    def write(self, data, timeout=None):
        """Record outgoing bytes."""
        if self.fail:
            raise OSError(self.fail)
        self.written.extend(data)
        return len(data)

    def read(self, size, timeout=None):
        """Pop up to ``size`` buffered bytes, raising a timeout when empty."""
        if self.fail:
            raise self.fail
        if not self.buffer:
            raise sys.modules["usb.core"].USBTimeoutError("timeout")
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


class _FakeInterface:
    """USB interface stand-in holding endpoints."""

    def __init__(self, number, cls, endpoints=()):
        self.bInterfaceNumber = number  # pylint: disable=invalid-name
        self.bInterfaceClass = cls  # pylint: disable=invalid-name
        self._endpoints = list(endpoints)

    def __iter__(self):
        return iter(self._endpoints)


class _FakeUsbDevice:
    """USB device stand-in exposing a CDC configuration."""

    def __init__(self, interfaces):
        self.bus = 3
        self.address = 42
        self._interfaces = interfaces
        self.detached = []
        self.control_transfers = []
        self.kernel_driver = True

    def get_active_configuration(self):
        """Return the interface list as the active configuration."""
        return self._interfaces

    def is_kernel_driver_active(self, number):
        """Report a driver bound to every interface."""
        return self.kernel_driver

    def detach_kernel_driver(self, number):
        """Record the detach."""
        self.detached.append(number)

    def ctrl_transfer(self, *args):
        """Record a control transfer."""
        self.control_transfers.append(args)


def _install_usb(monkeypatch, device, claim_error=None):
    """Install fake ``usb.core`` and ``usb.util`` modules and return the OUT endpoint."""
    core = types.ModuleType("usb.core")
    util = types.ModuleType("usb.util")
    package = types.ModuleType("usb")

    class USBError(Exception):
        """Fake pyusb error."""

        def __init__(self, message, errno=None):
            super().__init__(message)
            self.errno = errno

    class USBTimeoutError(USBError):
        """Fake pyusb timeout."""

    core.USBError = USBError
    core.USBTimeoutError = USBTimeoutError
    core.find = lambda **kwargs: device
    util.ENDPOINT_OUT = 0x00
    util.ENDPOINT_IN = 0x80
    util.endpoint_direction = lambda address: address & 0x80

    def claim_interface(dev, number):
        """Claim, or fail when the test asks for it."""
        if claim_error:
            raise OSError(claim_error)

    util.claim_interface = claim_interface
    util.release_interface = lambda dev, number: None
    util.dispose_resources = lambda dev: None
    package.core = core
    package.util = util
    monkeypatch.setitem(sys.modules, "usb", package)
    monkeypatch.setitem(sys.modules, "usb.core", core)
    monkeypatch.setitem(sys.modules, "usb.util", util)
    return core


def _cdc_device(in_buffer=b""):
    """Build a fake device with a CDC comm and data interface."""
    out_ep = _FakeEndpoint(0x02)
    in_ep = _FakeEndpoint(0x82, in_buffer)
    comm = _FakeInterface(0, usb_cdc.CDC_COMM_CLASS)
    data = _FakeInterface(1, usb_cdc.CDC_DATA_CLASS, [out_ep, in_ep])
    return _FakeUsbDevice([comm, data]), out_ep, in_ep


def test_usb_cdc_happy_path(monkeypatch):
    """The data interface is claimed, DTR raised and both endpoints found."""
    device, out_ep, _ = _cdc_device()
    _install_usb(monkeypatch, device)
    transport = UsbCdcTransport()
    assert transport.name == "usb:003:042"
    assert device.detached == [0, 1]
    assert device.control_transfers
    transport.write(b"version\n")
    assert out_ep.written == b"version\n"
    transport.close()
    transport.close()


def test_usb_cdc_matches_on_serial_number(monkeypatch):
    """A serial number narrows the libusb match."""
    device, _, _ = _cdc_device()
    core = _install_usb(monkeypatch, device)
    seen = {}

    def find(**kwargs):
        """Record the matcher."""
        seen.update(kwargs)
        return device

    core.find = find
    UsbCdcTransport(serial_number="VM1")
    assert seen["serial_number"] == "VM1"


def test_usb_cdc_without_comm_interface(monkeypatch):
    """A device exposing only the data interface skips the DTR handshake."""
    out_ep, in_ep = _FakeEndpoint(0x02), _FakeEndpoint(0x82)
    device = _FakeUsbDevice([_FakeInterface(1, usb_cdc.CDC_DATA_CLASS, [out_ep, in_ep])])
    _install_usb(monkeypatch, device)
    UsbCdcTransport()
    assert not device.control_transfers


def test_usb_cdc_close_tolerates_failures(monkeypatch):
    """Release and dispose failures never propagate out of close."""
    device, _, _ = _cdc_device()
    _install_usb(monkeypatch, device)
    transport = UsbCdcTransport()

    def boom(*args):
        """Fail every teardown call."""
        raise OSError("gone")

    transport._util.release_interface = boom
    transport._util.dispose_resources = boom
    transport.close()
    assert transport._claimed is None


def test_usb_cdc_read(monkeypatch):
    """Buffered bulk data is drained until the endpoint times out."""
    device, _, _ = _cdc_device(b"@version:1.0\n")
    _install_usb(monkeypatch, device)
    transport = UsbCdcTransport()
    assert transport.read(timeout=0.5) == b"@version:1.0\n"
    assert transport.read(timeout=0.01) == b""


def test_usb_cdc_read_ignores_recoverable_errors(monkeypatch):
    """Timeout-flavoured USB errors read as no data."""
    device, _, in_ep = _cdc_device()
    core = _install_usb(monkeypatch, device)
    transport = UsbCdcTransport()
    in_ep.fail = core.USBError("timed out", errno=110)
    assert transport.read(timeout=0.01) == b""


def test_usb_cdc_read_raises_on_fatal_error(monkeypatch):
    """Other USB errors surface as TransportError."""
    device, _, in_ep = _cdc_device()
    core = _install_usb(monkeypatch, device)
    transport = UsbCdcTransport()
    in_ep.fail = core.USBError("broken pipe", errno=32)
    with pytest.raises(TransportError, match="USB bulk read failed"):
        transport.read(timeout=0.01)


def test_usb_cdc_write_error(monkeypatch):
    """A failing bulk write surfaces as TransportError."""
    device, out_ep, _ = _cdc_device()
    _install_usb(monkeypatch, device)
    transport = UsbCdcTransport()
    out_ep.fail = "stall"
    with pytest.raises(TransportError, match="USB bulk write failed"):
        transport.write(b"x")


def test_usb_cdc_after_close(monkeypatch):
    """A closed transport refuses further traffic."""
    device, _, _ = _cdc_device()
    _install_usb(monkeypatch, device)
    transport = UsbCdcTransport()
    transport.close()
    with pytest.raises(TransportError, match="closed"):
        transport.write(b"x")
    with pytest.raises(TransportError, match="closed"):
        transport.read()


def test_usb_cdc_device_not_found(monkeypatch):
    """No matching device raises DeviceNotFoundError."""
    _install_usb(monkeypatch, None)
    with pytest.raises(DeviceNotFoundError, match="no USB device matching"):
        UsbCdcTransport()


def test_usb_cdc_backend_unavailable(monkeypatch):
    """A libusb backend that cannot enumerate is unavailable."""
    device, _, _ = _cdc_device()
    core = _install_usb(monkeypatch, device)

    def boom(**kwargs):
        """Refuse to enumerate."""
        raise OSError("no backend")

    core.find = boom
    with pytest.raises(TransportUnavailableError, match="libusb backend unavailable"):
        UsbCdcTransport()


def test_usb_cdc_missing_library(monkeypatch):
    """Without pyusb installed the backend is unavailable."""
    monkeypatch.setitem(sys.modules, "usb.core", None)
    with pytest.raises(TransportUnavailableError, match="pyusb is not installed"):
        UsbCdcTransport()


def test_usb_cdc_without_data_interface(monkeypatch):
    """A device with no CDC data interface cannot carry the shell."""
    device = _FakeUsbDevice([_FakeInterface(0, usb_cdc.CDC_COMM_CLASS)])
    _install_usb(monkeypatch, device)
    with pytest.raises(TransportError, match="no CDC data interface"):
        UsbCdcTransport()


def test_usb_cdc_without_bulk_endpoints(monkeypatch):
    """A data interface missing an endpoint direction is unusable."""
    data = _FakeInterface(1, usb_cdc.CDC_DATA_CLASS, [_FakeEndpoint(0x02)])
    _install_usb(monkeypatch, _FakeUsbDevice([data]))
    with pytest.raises(TransportError, match="missing bulk endpoints"):
        UsbCdcTransport()


def test_usb_cdc_claim_failure(monkeypatch):
    """An interface held by another process is an unavailable transport."""
    device, _, _ = _cdc_device()
    _install_usb(monkeypatch, device, claim_error="busy")
    with pytest.raises(TransportUnavailableError, match="cannot claim CDC data interface"):
        UsbCdcTransport()


def test_usb_cdc_skips_detach_when_disabled(monkeypatch):
    """Detaching the kernel driver is optional, and failures are ignored."""
    device, _, _ = _cdc_device()
    _install_usb(monkeypatch, device)
    transport = UsbCdcTransport(detach_kernel_driver=False)
    assert not device.detached
    transport.close()


def test_usb_cdc_tolerates_detach_and_dtr_failures(monkeypatch):
    """Best-effort setup steps never abort the open."""
    device, _, _ = _cdc_device()
    _install_usb(monkeypatch, device)

    def boom(*args):
        """Fail every optional setup call."""
        raise OSError("denied")

    device.is_kernel_driver_active = boom
    device.ctrl_transfer = boom
    assert UsbCdcTransport().name == "usb:003:042"


def test_usb_cdc_skips_detach_when_no_driver_bound(monkeypatch):
    """An unbound interface needs no detach."""
    device, _, _ = _cdc_device()
    device.kernel_driver = False
    _install_usb(monkeypatch, device)
    UsbCdcTransport()
    assert not device.detached
