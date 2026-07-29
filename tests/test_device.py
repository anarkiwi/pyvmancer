"""ProgramParameter scaling, the Videomancer facade, and backend selection."""

import json

import pytest

from pyvmancer import device as device_module
from pyvmancer.const import PARAM_MAX, PARAM_MIN
from pyvmancer.device import ProgramParameter, Videomancer, open_midi, open_shell
from pyvmancer.errors import DeviceNotFoundError, TransportUnavailableError, VmancerError
from pyvmancer.midi import MidiController
from pyvmancer.shell import ShellClient

from .conftest import FakeByteTransport, FakeMidiTransport

PROGRAM_INFO = {
    "name": "posterize",
    "parameters": [
        {"name": "Levels", "min": 0, "max": 7},
        {"name": "Mix", "min": 0, "max": 100},
        {"name": "Invert", "min": 0, "max": 1},
    ],
}


@pytest.mark.parametrize("minimum,maximum", [(0, 7), (0, 100), (0, 1), (-5, 5), (1, 1000)])
def test_program_parameter_round_trip(minimum, maximum):
    """Native values survive a trip through device units to within one step."""
    param = ProgramParameter(0, "p", minimum, maximum)
    step = (maximum - minimum) / PARAM_MAX
    for value in (minimum, (minimum + maximum) / 2, maximum):
        assert param.from_device(param.to_device(value)) == pytest.approx(value, abs=step)


@pytest.mark.parametrize("minimum,maximum", [(0, 7), (0, 100), (0, 1)])
def test_program_parameter_endpoints(minimum, maximum):
    """The native extremes land on the device extremes."""
    param = ProgramParameter(0, "p", minimum, maximum)
    assert param.to_device(minimum) == PARAM_MIN
    assert param.to_device(maximum) == PARAM_MAX
    assert param.from_device(PARAM_MIN) == pytest.approx(minimum)
    assert param.from_device(PARAM_MAX) == pytest.approx(maximum)


@pytest.mark.parametrize("value", [-100, 1000])
def test_program_parameter_clamps_native_values(value):
    """Values outside the native range clamp instead of extrapolating."""
    param = ProgramParameter(0, "p", 0, 7)
    assert param.to_device(value) in (PARAM_MIN, PARAM_MAX)


@pytest.mark.parametrize("value,expected", [(-10, PARAM_MIN), (99999, 7)])
def test_program_parameter_from_device_clamps(value, expected):
    """Device values outside 0..1023 clamp before mapping back."""
    assert ProgramParameter(0, "p", 0, 7).from_device(value) == pytest.approx(expected)


@pytest.mark.parametrize("minimum,maximum", [(5, 5), (10, 2)])
def test_program_parameter_degenerate_range(minimum, maximum):
    """A zero or inverted span has no usable scale, so both directions collapse."""
    param = ProgramParameter(0, "p", minimum, maximum)
    assert param.to_device(minimum) == PARAM_MIN
    assert param.from_device(500) == pytest.approx(minimum)


def test_program_parameter_repr():
    """The repr shows the 1-based slot and the native range."""
    assert repr(ProgramParameter(2, "Mix", 0, 100)) == "ProgramParameter(P3, 'Mix', 0..100)"


@pytest.fixture(name="fakes")
def fakes_fixture():
    """A (midi transport, byte transport) pair scripted with program info."""
    byte_transport = FakeByteTransport()
    byte_transport.on("program info", json.dumps(PROGRAM_INFO))
    return FakeMidiTransport(), byte_transport


@pytest.fixture(name="vm")
def vm_fixture(fakes):
    """A Videomancer with both links open onto fakes."""
    midi_transport, byte_transport = fakes
    return Videomancer(
        midi=MidiController(midi_transport), shell=ShellClient(byte_transport, timeout=0.2), info="INFO"
    )


def test_has_links(vm):
    """Both links report as present."""
    assert vm.has_midi and vm.has_shell
    assert vm.info == "INFO"


def test_missing_links_raise():
    """Accessing an absent link raises rather than silently doing nothing."""
    vm = Videomancer()
    assert not vm.has_midi and not vm.has_shell
    with pytest.raises(TransportUnavailableError, match="no MIDI link"):
        _ = vm.midi
    with pytest.raises(TransportUnavailableError, match="no serial link"):
        _ = vm.shell


def test_parameters_are_cached(vm, fakes):
    """The program info query happens once until a refresh is asked for."""
    first = vm.parameters()
    assert list(first) == ["Levels", "Mix", "Invert"]
    assert vm.parameters() is first
    assert fakes[1].written.count("program info") == 1
    refreshed = vm.parameters(refresh=True)
    assert refreshed is not first
    assert fakes[1].written.count("program info") == 2


def test_parameters_without_any(vm, fakes):
    """A program reporting no parameters yields an empty mapping."""
    fakes[1].on("program info", "{}")
    assert vm.parameters(refresh=True) == {}


@pytest.mark.parametrize("name", ["Levels", "levels", "LEVELS", "lEvElS"])
def test_parameter_lookup_is_case_insensitive(vm, name):
    """Exact and case-folded names both resolve."""
    assert vm.parameter(name).index == 0


def test_parameter_unknown_name(vm):
    """An unknown name lists what the program does offer."""
    with pytest.raises(VmancerError, match="has no parameter 'Nope'"):
        vm.parameter("Nope")


def test_set_named_writes_over_shell(vm, fakes):
    """By default a named parameter is written as a manual modulation value."""
    vm.set_named("Levels", 7)
    assert fakes[1].last == f"modulation set 0 {PARAM_MAX}"
    vm.set_named("Mix", 50)
    assert fakes[1].last == "modulation set 1 512"


def test_set_named_over_midi(vm, fakes):
    """``use_midi`` routes the scaled value to the CC path instead."""
    vm.set_named("Invert", 1, use_midi=True)
    assert fakes[0].data == bytes([0xB0, 2, 127, 0xB0, 34, 127])
    assert "modulation set" not in " ".join(fakes[1].written)


def test_set_source_by_name(vm, fakes):
    """Sources are assigned to a named parameter's slot."""
    vm.set_source("Mix", "free lfo")
    assert fakes[1].last == "modulation source 1 1"


def test_load_program_invalidates_parameter_cache(vm, fakes):
    """Loading a new program forces the next parameter query to re-fetch."""
    vm.parameters()
    vm.load_program("perlin", settle=0)
    assert fakes[1].last == "program load perlin"
    vm.parameters()
    assert fakes[1].written.count("program info") == 2


def test_play_and_stop_prefer_shell(vm, fakes):
    """With a serial link, transport control goes over the shell."""
    vm.play()
    assert fakes[1].last == "transport play"
    vm.stop()
    assert fakes[1].last == "transport stop"
    assert fakes[0].data == b""


def test_play_and_stop_fall_back_to_midi(fakes):
    """Without a serial link, transport control falls back to MIDI realtime."""
    vm = Videomancer(midi=MidiController(fakes[0]))
    vm.play()
    vm.stop()
    assert fakes[0].data == bytes([0xFA, 0xFC])


def test_midi_delegation(vm, fakes):
    """The facade forwards the MIDI-only calls to the controller."""
    vm.set_param(1, 1.0)
    vm.set_params({12: 0})
    vm.trigger(2, velocity=64)
    vm.select_preset(3)
    assert fakes[0].data.endswith(bytes([0x90, 1, 64, 0xC0, 3]))


def test_shell_delegation(vm, fakes):
    """The facade forwards the serial-only calls to the shell client."""
    fakes[1].on("programs list", '{"programs":["a"]}')
    fakes[1].on("program presets list", '{"factory":[]}')
    fakes[1].on("status", '{"product":"Videomancer"}')
    assert vm.programs() == ["a"]
    assert vm.presets() == {"factory": []}
    assert vm.status() == {"product": "Videomancer"}
    vm.set_bpm(120)
    assert fakes[1].last == "transport bpm 12000"


def test_close_closes_every_link(vm, fakes):
    """Closing releases both transports and drops the links."""
    vm.close()
    assert fakes[0].closed and fakes[1].closed
    assert not vm.has_midi and not vm.has_shell
    vm.close()


def test_context_manager_closes(fakes):
    """Leaving the context closes what is open."""
    with Videomancer(shell=ShellClient(fakes[1])) as vm:
        assert vm.has_shell
    assert fakes[1].closed


class _Recorder:
    """Records the arguments a patched opener was called with."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def test_open_uses_both_backends(monkeypatch, fakes):
    """``Videomancer.open`` combines whichever links come up."""
    midi = MidiController(fakes[0])
    shell = ShellClient(fakes[1])
    monkeypatch.setattr(device_module, "find_device", _Recorder(result="INFO"))
    monkeypatch.setattr(device_module, "open_midi", _Recorder(result=midi))
    monkeypatch.setattr(device_module, "open_shell", _Recorder(result=shell))
    vm = Videomancer.open(serial_number="VM1")
    assert vm.midi is midi and vm.shell is shell
    assert vm.info == "INFO"


def test_open_tolerates_a_failed_link_and_missing_info(monkeypatch, fakes):
    """A dead serial link still yields a MIDI-only session."""
    monkeypatch.setattr(device_module, "find_device", _Recorder(error=DeviceNotFoundError("none")))
    monkeypatch.setattr(device_module, "open_midi", _Recorder(result=MidiController(fakes[0])))
    monkeypatch.setattr(device_module, "open_shell", _Recorder(error=OSError("busy")))
    vm = Videomancer.open()
    assert vm.has_midi and not vm.has_shell
    assert vm.info is None


def test_open_skips_disabled_backends(monkeypatch, fakes):
    """Backends turned off by argument are never opened."""
    midi_opener = _Recorder(result=MidiController(fakes[0]))
    shell_opener = _Recorder(result=ShellClient(fakes[1]))
    monkeypatch.setattr(device_module, "find_device", _Recorder(result=None))
    monkeypatch.setattr(device_module, "open_midi", midi_opener)
    monkeypatch.setattr(device_module, "open_shell", shell_opener)
    vm = Videomancer.open(midi=False)
    assert not vm.has_midi and vm.has_shell
    assert not midi_opener.calls
    other = Videomancer.open(shell=False)
    assert other.has_midi and not other.has_shell
    assert len(shell_opener.calls) == 1


def test_open_raises_when_nothing_opens(monkeypatch):
    """With no link at all there is nothing to talk to."""
    monkeypatch.setattr(device_module, "find_device", _Recorder(error=DeviceNotFoundError("none")))
    monkeypatch.setattr(device_module, "open_midi", _Recorder(error=TransportUnavailableError("no midi")))
    monkeypatch.setattr(device_module, "open_shell", _Recorder(error=TransportUnavailableError("no tty")))
    with pytest.raises(TransportUnavailableError, match="no Videomancer MIDI or serial link"):
        Videomancer.open()


class _FakeTransport:
    """Stand-in transport that records its construction arguments."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def close(self):
        """No-op close."""


@pytest.mark.parametrize("prefer", ["bogus", "serial", ""])
def test_open_midi_rejects_unknown_backend(prefer):
    """Only auto/rawmidi/mido are valid MIDI backends."""
    with pytest.raises(ValueError, match="prefer must be auto/rawmidi/mido"):
        open_midi(prefer=prefer)


@pytest.mark.parametrize("prefer", ["bogus", "rawmidi", ""])
def test_open_shell_rejects_unknown_backend(prefer):
    """Only auto/tty/usb are valid serial backends."""
    with pytest.raises(ValueError, match="prefer must be auto/tty/usb"):
        open_shell(prefer=prefer)


def _patch_mido_transport(monkeypatch, factory):
    """Replace MidoTransport in the lazily-imported backend module."""
    from pyvmancer.transports import mido_port

    monkeypatch.setattr(mido_port, "MidoTransport", factory)


def test_open_midi_uses_mido_first(monkeypatch):
    """The default preference opens the named mido port."""
    _patch_mido_transport(monkeypatch, _FakeTransport)
    controller = open_midi(port="Videomancer MIDI 1", channel=3, high_resolution=False)
    assert isinstance(controller, MidiController)
    assert controller.transport.args == ("Videomancer MIDI 1",)
    assert controller.channel == 3
    assert controller.high_resolution is False


def test_open_midi_default_port_matches_product(monkeypatch):
    """With no port given, the first mido port naming the product is used."""
    _patch_mido_transport(monkeypatch, _FakeTransport)
    monkeypatch.setattr("mido.get_output_names", lambda: ["Midi Through", "Videomancer:0"])
    assert open_midi().transport.args == ("Videomancer:0",)


def test_open_midi_default_port_not_found(monkeypatch):
    """No matching port fails the mido backend outright when it is required."""
    _patch_mido_transport(monkeypatch, _FakeTransport)
    monkeypatch.setattr("mido.get_output_names", lambda: ["Midi Through"])
    with pytest.raises(TransportUnavailableError, match="no mido port matching"):
        open_midi(prefer="mido")


def test_open_midi_backend_unavailable(monkeypatch):
    """A mido backend that cannot enumerate ports is reported as unavailable."""
    _patch_mido_transport(monkeypatch, _FakeTransport)

    def boom():
        raise RuntimeError("no backend")

    monkeypatch.setattr("mido.get_output_names", boom)
    with pytest.raises(TransportUnavailableError, match="mido backend unavailable"):
        open_midi(prefer="mido")


def test_open_midi_mido_failure_is_raised_when_requested(monkeypatch):
    """``prefer="mido"`` does not fall back."""
    _patch_mido_transport(monkeypatch, _Recorder(error=TransportUnavailableError("nope")))
    with pytest.raises(TransportUnavailableError, match="nope"):
        open_midi(port="x", prefer="mido")


def test_open_midi_falls_back_to_rawmidi(monkeypatch):
    """When mido fails, auto mode tries the ALSA rawmidi node."""
    _patch_mido_transport(monkeypatch, _Recorder(error=OSError("no alsa")))
    monkeypatch.setattr(device_module, "RawMidiTransport", _FakeTransport)
    assert open_midi(port="/dev/snd/fake").transport.args == ("/dev/snd/fake",)


def test_open_midi_rawmidi_uses_discovery(monkeypatch):
    """Without an explicit port the rawmidi node comes from discovery."""
    info = type("Info", (), {"rawmidi": "/dev/snd/discovered"})()
    monkeypatch.setattr(device_module, "find_device", _Recorder(result=info))
    monkeypatch.setattr(device_module, "RawMidiTransport", _FakeTransport)
    assert open_midi(prefer="rawmidi").transport.args == ("/dev/snd/discovered",)


def test_open_midi_reports_every_failure(monkeypatch):
    """Both backends failing produces one aggregated error."""
    _patch_mido_transport(monkeypatch, _Recorder(error=OSError("no alsa")))
    monkeypatch.setattr(device_module, "RawMidiTransport", _Recorder(error=OSError("no node")))
    with pytest.raises(TransportUnavailableError, match="no usable MIDI backend"):
        open_midi(port="/dev/snd/fake")


def _patch_serial_transport(monkeypatch, factory):
    """Replace SerialTransport in the lazily-imported backend module."""
    from pyvmancer.transports import serial_tty

    monkeypatch.setattr(serial_tty, "SerialTransport", factory)


def _patch_usb_transport(monkeypatch, factory):
    """Replace UsbCdcTransport in the lazily-imported backend module."""
    from pyvmancer.transports import usb_cdc

    monkeypatch.setattr(usb_cdc, "UsbCdcTransport", factory)


def test_open_shell_uses_tty_first(monkeypatch):
    """The default preference opens the given tty."""
    _patch_serial_transport(monkeypatch, _FakeTransport)
    client = open_shell(port="/dev/ttyFAKE", timeout=1.5)
    assert isinstance(client, ShellClient)
    assert client.transport.args == ("/dev/ttyFAKE",)
    assert client.timeout == 1.5


def test_open_shell_tty_uses_discovery(monkeypatch):
    """Without an explicit port the tty comes from discovery."""
    info = type("Info", (), {"tty": "/dev/ttyDISCOVERED"})()
    monkeypatch.setattr(device_module, "find_device", _Recorder(result=info))
    _patch_serial_transport(monkeypatch, _FakeTransport)
    assert open_shell().transport.args == ("/dev/ttyDISCOVERED",)


def test_open_shell_tty_failure_is_raised_when_requested(monkeypatch):
    """``prefer="tty"`` does not fall back."""
    _patch_serial_transport(monkeypatch, _Recorder(error=OSError("busy")))
    with pytest.raises(OSError, match="busy"):
        open_shell(port="/dev/ttyFAKE", prefer="tty")


def test_open_shell_falls_back_to_usb(monkeypatch):
    """When the tty path fails, auto mode claims the CDC interface over libusb."""
    _patch_serial_transport(monkeypatch, _Recorder(error=TransportUnavailableError("no tty")))
    _patch_usb_transport(monkeypatch, _FakeTransport)
    client = open_shell(serial_number="VM1")
    assert client.transport.kwargs["serial_number"] == "VM1"


def test_open_shell_usb_only_skips_the_tty_path(monkeypatch):
    """``prefer="usb"`` goes straight to libusb."""
    _patch_serial_transport(monkeypatch, _Recorder(error=AssertionError("tty must not be tried")))
    _patch_usb_transport(monkeypatch, _FakeTransport)
    assert open_shell(prefer="usb").transport.kwargs["serial_number"] is None


def test_open_shell_reports_every_failure(monkeypatch):
    """Both backends failing produces one aggregated error."""
    _patch_serial_transport(monkeypatch, _Recorder(error=TransportUnavailableError("no tty")))
    _patch_usb_transport(monkeypatch, _Recorder(error=TransportUnavailableError("no usb")))
    with pytest.raises(TransportUnavailableError, match="no usable serial backend"):
        open_shell()
