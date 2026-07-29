"""MidiController wire format, validation and clock generation."""

import numpy as np
import pytest

from pyvmancer.const import CC_LSB_OFFSET, MIDI_CLOCK_PPQN, PARAM_COUNT, PARAM_DEFAULTS
from pyvmancer.errors import VmancerError
from pyvmancer.midi import MidiController

from .conftest import FakeMidiTransport

CC_STATUS = 0xB0
NOTE_ON = 0x90
NOTE_OFF = 0x80
PROGRAM_CHANGE = 0xC0
CLOCK = 0xF8
START = 0xFA
STOP = 0xFC
CONTINUE = 0xFB


def test_set_param_high_resolution_emits_msb_then_lsb(midi, midi_transport):
    """0.75 is 767 device units, sent as CC0=95 then CC32=123."""
    midi.set_param(1, 0.75)
    assert midi_transport.data == bytes([CC_STATUS, 0, 95, CC_STATUS, CC_LSB_OFFSET, 123])
    assert midi.get_param(1) == 767


def test_set_param_low_resolution_emits_single_cc(midi_transport):
    """Without high resolution only the 7-bit MSB controller is sent."""
    controller = MidiController(midi_transport, high_resolution=False)
    controller.set_param(1, 0.75)
    assert midi_transport.data == bytes([CC_STATUS, 0, 95])


def test_high_resolution_skips_lsb_when_it_would_exceed_cc_range(midi, midi_transport):
    """An MSB controller above 95 has no room for its +32 LSB partner."""
    midi.assign_cc(1, 100)
    midi.set_param(1, 1.0)
    assert midi_transport.data == bytes([CC_STATUS, 100, 127])


@pytest.mark.parametrize("param", range(7, 12))
@pytest.mark.parametrize("value,expected", [(0.4, 1023), (0.0, 0), (True, 1023), (False, 0), (7, 1023)])
def test_toggle_params_coerce_to_extremes(midi, param, value, expected):
    """Slots 7-11 are toggles, so any truthy value snaps to full scale."""
    midi.set_param(param, value)
    assert midi.get_param(param) == expected


def test_select_preset_emits_program_change(midi, midi_transport):
    """Preset recall is a plain program change."""
    midi.select_preset(7)
    assert midi_transport.data == bytes([PROGRAM_CHANGE, 7])


@pytest.mark.parametrize("program", [-1, 128])
def test_select_preset_range(midi, program):
    """Program numbers are 0..127."""
    with pytest.raises(ValueError, match="program must be"):
        midi.select_preset(program)


@pytest.mark.parametrize("param", range(1, PARAM_COUNT + 1))
def test_trigger_and_release_use_notes_zero_to_eleven(midi, midi_transport, param):
    """Parameter N is triggered by note N-1."""
    midi.trigger(param).release(param)
    assert midi_transport.data == bytes([NOTE_ON, param - 1, 127, NOTE_OFF, param - 1, 0])


def test_trigger_velocity(midi, midi_transport):
    """Velocity is passed through to the note-on."""
    midi.trigger(3, velocity=64)
    assert midi_transport.data == bytes([NOTE_ON, 2, 64])


def test_pulse_triggers_then_releases(midi, midi_transport):
    """A pulse is a note-on/note-off pair."""
    midi.pulse(1, duration=0.0)
    assert midi_transport.data == bytes([NOTE_ON, 0, 127, NOTE_OFF, 0, 0])


def test_all_notes_off_releases_every_slot(midi, midi_transport):
    """Every modulator note is released once."""
    midi.all_notes_off()
    assert midi_transport.data == b"".join(bytes([NOTE_OFF, note, 0]) for note in range(PARAM_COUNT))


def test_realtime_messages(midi, midi_transport):
    """Start, stop, continue and clock are single-byte realtime messages."""
    midi.start().stop().cont().clock_tick()
    assert midi_transport.data == bytes([START, STOP, CONTINUE, CLOCK])


def test_clock_tick_count(midi, midi_transport):
    """A tick count emits that many clock bytes in one transport write."""
    midi.clock_tick(5)
    assert midi_transport.sent == [bytes([CLOCK] * 5)]


def test_channel_none_targets_channel_zero(midi, midi_transport):
    """Omni mode addresses channel index 0."""
    midi.select_preset(0)
    assert midi_transport.data[0] == PROGRAM_CHANGE


def test_channel_five_targets_index_four(midi_transport):
    """A 1-based channel maps to the 0-based wire nibble."""
    controller = MidiController(midi_transport, channel=5)
    controller.select_preset(1).trigger(1)
    assert midi_transport.data == bytes([PROGRAM_CHANGE | 4, 1, NOTE_ON | 4, 0, 127])


@pytest.mark.parametrize("channel", [0, 17, -3])
def test_channel_out_of_range(midi_transport, channel):
    """Channels are 1..16 or None."""
    controller = MidiController(midi_transport, channel=channel)
    with pytest.raises(ValueError, match="channel must be"):
        controller.select_preset(0)


@pytest.mark.parametrize("param", [0, PARAM_COUNT + 1])
def test_param_validation(midi, param):
    """Every parameter-taking method validates the slot number."""
    for call in (midi.set_param, midi.trigger):
        with pytest.raises(ValueError, match="parameter must be"):
            call(param, 0)
    with pytest.raises(ValueError, match="parameter must be"):
        midi.release(param)
    with pytest.raises(ValueError, match="parameter must be"):
        midi.get_param(param)


def test_assign_cc_rejects_collision(midi):
    """A CC already owned by another parameter cannot be stolen."""
    with pytest.raises(VmancerError, match="already assigned to parameter 1"):
        midi.assign_cc(2, 0)


def test_assign_cc_allows_same_parameter(midi):
    """Re-assigning a parameter to the CC it already owns is a no-op."""
    assert midi.assign_cc(1, 0) is midi
    midi.assign_cc(1, 64)
    assert midi.cc_map[0] == 64
    midi.assign_cc(2, 0)
    assert midi.cc_map[1] == 0


@pytest.mark.parametrize("cc", [-1, 128])
def test_assign_cc_range(midi, cc):
    """CC numbers are 0..127."""
    with pytest.raises(ValueError, match="cc must be"):
        midi.assign_cc(1, cc)


def test_assign_cc_validates_param(midi):
    """assign_cc validates the parameter slot too."""
    with pytest.raises(ValueError, match="parameter must be"):
        midi.assign_cc(99, 5)


def test_cc_map_is_a_copy(midi):
    """Mutating the returned map does not change the controller."""
    mapping = midi.cc_map
    mapping[0] = 99
    assert midi.cc_map[0] == 0


def test_state_is_a_copy_and_defaults(midi):
    """State starts at the program defaults and is handed out by value."""
    assert np.array_equal(midi.state, np.array(PARAM_DEFAULTS))
    state = midi.state
    state[0] = 1
    assert midi.state[0] == PARAM_DEFAULTS[0]


@pytest.mark.parametrize("cc_map", [list(range(11)), list(range(13))])
def test_cc_map_length_validated(midi_transport, cc_map):
    """A custom CC map must cover every parameter."""
    with pytest.raises(ValueError, match="cc_map must have"):
        MidiController(midi_transport, cc_map=cc_map)


@pytest.mark.parametrize("bad", [-1, 200])
def test_cc_map_values_validated(midi_transport, bad):
    """Custom CC map entries stay inside the CC range."""
    with pytest.raises(ValueError, match="cc_map entries must be"):
        MidiController(midi_transport, cc_map=[bad] + list(range(1, PARAM_COUNT)))


def test_custom_cc_map_is_used(midi_transport):
    """Parameters are sent on their mapped controller."""
    controller = MidiController(midi_transport, cc_map=list(range(20, 32)), high_resolution=False)
    controller.set_param(1, 1.0)
    assert midi_transport.data == bytes([CC_STATUS, 20, 127])


def test_set_params_from_mapping(midi, midi_transport):
    """A mapping addresses parameters by number."""
    midi.set_params({1: 1.0, 12: 0})
    assert midi.get_param(1) == 1023
    assert midi.get_param(12) == 0
    assert len(midi_transport.sent) == 2


def test_set_params_from_sequence(midi):
    """A 12-element sequence is applied in slot order."""
    midi.set_params([0] * PARAM_COUNT)
    assert np.array_equal(midi.state, np.zeros(PARAM_COUNT, dtype=np.int64))


def test_reset_params_restores_defaults(midi, midi_transport):
    """Reset sends every parameter back to its program default."""
    midi.set_params([0] * PARAM_COUNT)
    midi_transport.sent.clear()
    midi.reset_params()
    assert np.array_equal(midi.state, np.array(PARAM_DEFAULTS))
    assert len(midi_transport.sent) == PARAM_COUNT


def test_knob_toggle_fader_helpers(midi):
    """The panel helpers map onto their parameter slots."""
    midi.knob(6, 1.0)
    midi.toggle(5, True)
    midi.fader(0.0)
    assert midi.get_param(6) == 1023
    assert midi.get_param(11) == 1023
    assert midi.get_param(12) == 0


@pytest.mark.parametrize("index", [0, 7])
def test_knob_index_range(midi, index):
    """Knobs are 1..6."""
    with pytest.raises(ValueError, match="knob index must be"):
        midi.knob(index, 0)


@pytest.mark.parametrize("index", [0, 6])
def test_toggle_index_range(midi, index):
    """Toggles are 1..5."""
    with pytest.raises(ValueError, match="toggle index must be"):
        midi.toggle(index, True)


def test_run_clock_emits_expected_tick_count(midi, midi_transport):
    """Two beats at 6000 BPM is 48 ticks preceded by a start byte."""
    midi.run_clock(6000, 2)
    data = midi_transport.data
    assert data[0] == START
    assert data.count(bytes([CLOCK])) == 2 * MIDI_CLOCK_PPQN
    assert len(data) == 1 + 2 * MIDI_CLOCK_PPQN


def test_run_clock_without_start(midi, midi_transport):
    """``start=False`` suppresses the MIDI Start byte."""
    midi.run_clock(6000, 0.5)
    midi_transport.sent.clear()
    midi.run_clock(6000, 0.5, start=False)
    assert midi_transport.data == bytes([CLOCK] * (MIDI_CLOCK_PPQN // 2))


@pytest.mark.parametrize("bpm", [0, -120])
def test_run_clock_rejects_non_positive_bpm(midi, bpm):
    """Tempo must be positive."""
    with pytest.raises(ValueError, match="bpm must be positive"):
        midi.run_clock(bpm, 1)


def test_receive_parses_messages():
    """Bytes coming back from the device are decoded into mido messages."""
    transport = FakeMidiTransport(bytes([CC_STATUS, 1, 64, CLOCK]))
    controller = MidiController(transport)
    messages = controller.receive(timeout=0.25)
    assert [m.type for m in messages] == ["control_change", "clock"]
    assert transport.timeouts == [0.25]


def test_receive_empty(midi):
    """No pending bytes yields no messages."""
    assert midi.receive() == []


def test_close_and_context_manager(midi_transport):
    """Closing the controller closes its transport, as does leaving the context."""
    with MidiController(midi_transport) as controller:
        assert controller.transport is midi_transport
    assert midi_transport.closed


def test_transport_name_defaults_to_class_name(midi_transport):
    """The base transport names itself after its class."""
    assert midi_transport.name == "FakeMidiTransport"
    with midi_transport as entered:
        assert entered is midi_transport
