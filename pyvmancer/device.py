"""High-level Videomancer facade combining the MIDI and serial control paths."""

from .const import PARAM_MAX, PARAM_MIN, USB_PID, USB_VID
from .discovery import find_device
from .errors import DeviceNotFoundError, TransportUnavailableError, VmancerError
from .midi import MidiController
from .shell import ShellClient
from .transports.rawmidi import RawMidiTransport


def open_midi(serial_number=None, port=None, channel=None, high_resolution=True, prefer="auto"):
    """Open a :class:`~pyvmancer.midi.MidiController` for an attached device.

    ``prefer`` selects the backend: ``"mido"`` (portable, needs a working mido
    backend such as python-rtmidi), ``"rawmidi"`` (Linux ALSA character device,
    no libasound required), or ``"auto"`` to try mido then fall back to rawmidi.
    """
    if prefer not in ("auto", "rawmidi", "mido"):
        raise ValueError(f"prefer must be auto/rawmidi/mido, got {prefer!r}")
    errors = []
    if prefer in ("auto", "mido"):
        from .transports.mido_port import MidoTransport

        try:
            return MidiController(
                MidoTransport(port or _default_mido_port()),
                channel=channel,
                high_resolution=high_resolution,
            )
        except (TransportUnavailableError, OSError) as err:
            if prefer == "mido":
                raise
            errors.append(err)
    try:
        return MidiController(
            RawMidiTransport(port or find_device(serial_number).rawmidi),
            channel=channel,
            high_resolution=high_resolution,
        )
    except (TransportUnavailableError, DeviceNotFoundError, TypeError, OSError) as err:
        errors.append(err)
        raise TransportUnavailableError(f"no usable MIDI backend: {errors}") from err


def _default_mido_port():
    """Return the first mido output port that looks like a Videomancer."""
    import mido

    from .const import USB_PRODUCT

    try:
        names = mido.get_output_names()
    except Exception as err:
        raise TransportUnavailableError(f"mido backend unavailable: {err}") from err
    for name in names:
        if USB_PRODUCT.lower() in name.lower():
            return name
    raise TransportUnavailableError(f"no mido port matching {USB_PRODUCT!r}")


def open_shell(serial_number=None, port=None, timeout=5.0, prefer="auto"):
    """Open a :class:`~pyvmancer.shell.ShellClient` over the CDC serial link.

    ``prefer`` selects the backend: ``"tty"`` (``/dev/ttyACM*`` via pyserial),
    ``"usb"`` (libusb against ``/dev/bus/usb``, for environments with no tty
    nodes), or ``"auto"`` to try tty then fall back to usb.
    """
    if prefer not in ("auto", "tty", "usb"):
        raise ValueError(f"prefer must be auto/tty/usb, got {prefer!r}")
    errors = []
    if prefer in ("auto", "tty"):
        from .transports.serial_tty import SerialTransport

        try:
            return ShellClient(SerialTransport(port or find_device(serial_number).tty), timeout=timeout)
        except (TransportUnavailableError, DeviceNotFoundError, TypeError, OSError) as err:
            if prefer == "tty":
                raise
            errors.append(err)
    from .transports.usb_cdc import UsbCdcTransport

    try:
        return ShellClient(
            UsbCdcTransport(vid=USB_VID, pid=USB_PID, serial_number=serial_number), timeout=timeout
        )
    except TransportUnavailableError as err:
        errors.append(err)
        raise TransportUnavailableError(f"no usable serial backend: {errors}") from err


class ProgramParameter:
    """One named parameter of the loaded program, with its native range."""

    __slots__ = ("index", "name", "minimum", "maximum")

    def __init__(self, index, name, minimum, maximum):
        self.index = index
        self.name = name
        self.minimum = minimum
        self.maximum = maximum

    def to_device(self, value):
        """Map a native value onto the 0..1023 device range."""
        span = self.maximum - self.minimum
        if span <= 0:
            return PARAM_MIN
        clamped = min(max(value, self.minimum), self.maximum)
        return int(round((clamped - self.minimum) / span * PARAM_MAX))

    def from_device(self, value):
        """Map a 0..1023 device value back to the native range."""
        span = self.maximum - self.minimum
        if span <= 0:
            return self.minimum
        return self.minimum + (min(max(value, PARAM_MIN), PARAM_MAX) / PARAM_MAX) * span

    def __repr__(self):
        return f"ProgramParameter(P{self.index + 1}, {self.name!r}, {self.minimum}..{self.maximum})"


class Videomancer:
    """A Videomancer addressed over MIDI, serial, or both.

    Either link may be absent; every method raises
    :class:`~pyvmancer.errors.TransportUnavailableError` if the link it needs is
    not open, so a MIDI-only session still works for parameters and transport.
    """

    def __init__(self, midi=None, shell=None, info=None):
        self._midi = midi
        self._shell = shell
        self.info = info
        self._parameters = None

    @classmethod
    def open(cls, serial_number=None, midi=True, shell=True, channel=None, high_resolution=True):
        """Open whichever links are available, ignoring ones that fail."""
        info = None
        try:
            info = find_device(serial_number)
        except Exception:
            pass
        midi_ctl = None
        shell_client = None
        if midi:
            try:
                midi_ctl = open_midi(
                    serial_number=serial_number, channel=channel, high_resolution=high_resolution
                )
            except Exception:
                midi_ctl = None
        if shell:
            try:
                shell_client = open_shell(serial_number=serial_number)
            except Exception:
                shell_client = None
        if midi_ctl is None and shell_client is None:
            raise TransportUnavailableError("no Videomancer MIDI or serial link could be opened")
        return cls(midi=midi_ctl, shell=shell_client, info=info)

    @property
    def midi(self):
        """The MIDI controller, or raise if the MIDI link is not open."""
        if self._midi is None:
            raise TransportUnavailableError("no MIDI link is open")
        return self._midi

    @property
    def shell(self):
        """The serial shell client, or raise if the serial link is not open."""
        if self._shell is None:
            raise TransportUnavailableError("no serial link is open")
        return self._shell

    @property
    def has_midi(self):
        """True when the MIDI link is open."""
        return self._midi is not None

    @property
    def has_shell(self):
        """True when the serial link is open."""
        return self._shell is not None

    def set_param(self, param, value):
        """Set a parameter over MIDI."""
        return self.midi.set_param(param, value)

    def set_params(self, values):
        """Set several parameters over MIDI."""
        return self.midi.set_params(values)

    def trigger(self, param, velocity=127):
        """Fire a parameter's note-triggered operator."""
        return self.midi.trigger(param, velocity=velocity)

    def select_preset(self, program):
        """Recall a preset by MIDI program change."""
        return self.midi.select_preset(program)

    def load_program(self, name, settle=1.5):
        """Load an FPGA program by name (serial only)."""
        result = self.shell.load_program(name, settle=settle)
        self._parameters = None
        return result

    def parameters(self, refresh=False):
        """Named parameters of the loaded program, indexed by name (serial only).

        The device reports each parameter's native range, e.g. ``Posterize``
        ``0..7``; values are assumed to map linearly onto ``0..1023``.
        """
        if self._parameters is None or refresh:
            info = self.shell.program_info()
            self._parameters = {
                entry["name"]: ProgramParameter(index, entry["name"], entry["min"], entry["max"])
                for index, entry in enumerate(info.get("parameters", []))
            }
        return self._parameters

    def parameter(self, name):
        """Look up one named parameter, case-insensitively."""
        params = self.parameters()
        if name in params:
            return params[name]
        lowered = name.lower()
        for key, value in params.items():
            if key.lower() == lowered:
                return value
        raise VmancerError(f"program has no parameter {name!r}; have {sorted(params)}")

    def set_named(self, name, value, use_midi=False):
        """Set a program parameter by name, in the program's native units.

        Writes over serial by default, which sets the manual value directly;
        ``use_midi=True`` sends it as a CC offset instead.
        """
        param = self.parameter(name)
        device_value = param.to_device(value)
        if use_midi:
            return self.midi.set_param(param.index + 1, device_value)
        return self.shell.set_modulation(param.index, device_value)

    def set_source(self, name, source):
        """Assign a modulation operator to a named parameter (serial only)."""
        return self.shell.set_source(self.parameter(name).index, source)

    def programs(self):
        """List installed FPGA programs (serial only)."""
        return self.shell.programs()

    def presets(self):
        """List factory and user presets (serial only)."""
        return self.shell.presets()

    def play(self):
        """Start playback, preferring the serial transport command."""
        return self.shell.play() if self._shell else self.midi.start()

    def stop(self):
        """Stop playback, preferring the serial transport command."""
        return self.shell.stop() if self._shell else self.midi.stop()

    def set_bpm(self, bpm):
        """Set internal tempo (serial only)."""
        return self.shell.set_bpm(bpm)

    def status(self):
        """Device status dict (serial only)."""
        return self.shell.status()

    def close(self):
        """Close every open link."""
        for link in (self._midi, self._shell):
            if link is not None:
                link.close()
        self._midi = None
        self._shell = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
