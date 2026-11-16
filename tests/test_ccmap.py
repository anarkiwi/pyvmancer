"""CC-map comparison, assignment and CLI against a fake whose settings keys and live map can disagree."""

import json
from contextlib import nullcontext

import pytest

from pyvmancer.const import ShellErrorCode

from pyvmancer import cli
from pyvmancer.ccmap import (
    assign_cc_map,
    cc_map_report,
    encode_setting,
    expected_cc_map,
    parse_cc_map,
    read_cc_settings,
    reassert_cc_map,
    sink_cc,
    sink_cc_of,
    verify_cc_map,
)
from pyvmancer.const import ASSIGN_SINK_CCS, CC_MAP_UNASSIGNED, PARAM_COUNT
from pyvmancer.diagnose import diagnose_cc_map
from pyvmancer.errors import CcMapError, VmancerError
from pyvmancer.latency import CcProbeReport
from pyvmancer.midi import MidiController
from pyvmancer.device import Videomancer
from pyvmancer.shell import ShellClient

from .conftest import FakeByteTransport, FakeCcMap, FakeMidiTransport

#: ``modulation cc-map`` as read from a unit whose renders came out as passthrough.
OBSERVED = {
    "assignments": [{"msb": m, "lsb": m + 32, "auto": True} for m in (0, 1, 2, 3, 4, 5, 0, 7, 8, 0, 10)]
    + [None]
}
CORRUPT = [0, 1, 2, 3, 4, 5, 0, 7, 8, 0, 10, None]
DEFAULT_WRITES = [(200 + i, v) for i in range(PARAM_COUNT) for v in (CC_MAP_UNASSIGNED, 0x1FF00 | i)]


def test_observed_corruption_names_every_fault():
    """P7 and P10 on P1's CC, and P12 unassigned, are each named with what was expected."""
    report = cc_map_report(FakeCcMap(CORRUPT))
    assert report.actual == parse_cc_map(OBSERVED)
    assert report.mismatched == (7, 10) and report.unassigned == (12,) and report.wrong == (7, 10, 12)
    assert report.shared == {1: (7, 10), 7: (1, 10), 10: (1, 7)}
    finding = diagnose_cc_map(report)
    assert not finding.ok
    assert (
        finding.detail
        == "P7 on 0/32, want 6/38; P10 on 0/32, want 9/41; P1, P7, P10 share a CC; P12 unassigned"
    )
    assert "vmancer cc-map --restore --yes" in finding.fix
    assert report.summary() == "P1 CC0, P7 CC0, P10 CC0, P12 unassigned"


def test_correct_map_is_ok():
    """The default map matches the default controller, LSB included."""
    report = cc_map_report(FakeCcMap())
    finding = diagnose_cc_map(report)
    assert report.ok and finding.ok and not finding.fix
    assert finding.detail.startswith("P1..P12 on msb/lsb 0/32 1/33") and finding.detail.endswith("11/43")
    assert json.loads(json.dumps(report.as_dict()))["actual"][11] == {"msb": 11, "lsb": 43}


def test_expected_map_follows_the_controller():
    """A custom 7-bit map is expected with no LSB, and a device LSB of 255 reads as none."""
    midi = MidiController(None, cc_map=list(range(40, 52)), high_resolution=False)
    assert expected_cc_map(midi) == tuple((m, None) for m in range(40, 52))
    device = FakeCcMap(range(40, 52))
    assert device.cc_map()["assignments"][0]["lsb"] == 255
    report = cc_map_report(device, midi)
    assert report.ok and not cc_map_report(device).ok
    assert "assign_cc_map" in diagnose_cc_map(cc_map_report(FakeCcMap(), midi)).fix


def test_lsb_mismatch_counts_only_where_an_lsb_is_sent():
    """A wrong LSB fails a 14-bit slot and is ignored for a 7-bit one."""
    raw = {"assignments": [{"msb": m, "lsb": 99} for m in range(PARAM_COUNT)]}
    shell = FakeCcMap()
    shell.cc_map = lambda: raw
    assert cc_map_report(shell).mismatched == tuple(range(1, 13))
    assert cc_map_report(shell, MidiController(None, high_resolution=False)).ok


@pytest.mark.parametrize("raw", [{}, {"assignments": [None] * 3}, "ok"])
def test_absent_map_reports_nothing(raw):
    """Firmware without a 12-slot map yields no report, and verification raises."""
    shell = FakeCcMap()
    shell.cc_map = lambda: raw
    assert cc_map_report(shell) is None
    with pytest.raises(VmancerError, match="no 12-slot CC map"):
        verify_cc_map(shell)


def test_assign_writes_every_slot_even_when_already_right():
    """The write is unconditional: every key is cleared then set, and the map read back."""
    device = FakeCcMap()
    assigned = assign_cc_map(device)
    assert device.writes == DEFAULT_WRITES
    assert assigned.after.ok and assigned.summary() == "cc-map: assigned P1..P12 -> CC0..11 (was: ok)"


def test_assign_fixes_a_live_slot_whose_key_already_holds_the_target():
    """A capture leaves its key unchanged and same-value writes are ignored, so clearing first fixes it."""
    device = FakeCcMap(CORRUPT, keys=[encode_setting(m) for m in range(PARAM_COUNT)])
    device.set_setting(209, encode_setting(9))
    assert device.live[9] == 0
    assigned = assign_cc_map(device)
    assert device.live == list(range(PARAM_COUNT))
    assert assigned.summary().endswith("(was: P1 CC0, P7 CC0, P10 CC0, P12 unassigned)")


def test_dry_run_plans_without_writing():
    """``apply=False`` reads only and lists both writes per key."""
    device = FakeCcMap(CORRUPT)
    planned = assign_cc_map(device, apply=False)
    data = planned.as_dict()
    assert not device.writes and data["dry_run"] and data["after"] is None
    assert data["commands"][:2] == ["settings set 200 131071", "settings set 200 130816"]
    assert len(data["commands"]) == 24 and planned.summary().startswith("cc-map: would assign")
    assert data["writes"][11] == {"key": 211, "old": CC_MAP_UNASSIGNED, "new": 0x1FF0B}


@pytest.mark.parametrize("value", [0x12345, 0x1FE00, 5])
def test_unknown_encoding_is_refused_before_any_write(value):
    """A key outside the observed encoding means a different firmware; nothing is written."""
    device = FakeCcMap()
    device.keys[3] = value
    with pytest.raises(VmancerError, match="settings key 203 .* refusing to write"):
        assign_cc_map(device)
    assert not device.writes


def test_map_that_does_not_take_raises():
    """A device that ignores the writes is an error naming the wrong slots."""
    device = FakeCcMap(CORRUPT)
    device.set_setting = lambda key, value: None
    with pytest.raises(CcMapError, match=r"P1 CC0, P7 CC0, P10 CC0, P12 unassigned"):
        assign_cc_map(device)


def test_verify_raises_on_any_change():
    """A pure read-back: a slot captured mid-pass fails verification without writing."""
    device = FakeCcMap()
    assert verify_cc_map(device).ok
    device.live[11] = 0
    with pytest.raises(CcMapError, match="P1 CC0, P12 CC0"):
        verify_cc_map(device)
    assert not device.writes


@pytest.mark.parametrize(
    "payload,match",
    [
        ('{"key":200}', "lacks fields value"),
        ('"x"', "lacks fields value"),
        ("130816", "lacks fields value"),
        ('{"key":200,"value":"x"}', "is not an integer"),
        ('{"key":200,"value":true}', "is not an integer"),
    ],
)
def test_settings_reply_without_an_integer_is_refused(payload, match):
    """A ``settings get`` reply that is not an object with an integer value is an error, not a guess."""
    device = FakeCcMap()
    device.get_setting = lambda key: payload
    with pytest.raises(VmancerError, match=match):
        read_cc_settings(device)


@pytest.fixture(name="device")
def device_fixture(monkeypatch):
    """A fake device behind ``vmancer``'s serial opener."""
    transport = FakeByteTransport()
    monkeypatch.setattr(cli, "open_shell", lambda **_: ShellClient(transport))
    return transport


def test_cli_shows_and_fails_on_a_corrupt_map(device, capsys):
    """``vmancer cc-map`` prints the comparison and exits 1 on a mismatch."""
    device.cc = FakeCcMap(CORRUPT)
    assert cli.main(["cc-map"]) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["unassigned"] == [12] and "FAIL midi.cc_map" in out.err
    device.cc = FakeCcMap()
    assert cli.main(["cc-map"]) == 0


def test_cli_restore_is_a_dry_run_without_yes(device, capsys):
    """``--restore`` alone plans; ``--restore --yes`` writes and verifies."""
    device.cc = FakeCcMap(CORRUPT)
    assert cli.main(["cc-map", "--restore"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "re-run with --yes to write"
    assert not device.cc.writes
    assert cli.main(["cc-map", "--restore", "--yes"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["after"]["ok"] and "cc-map: assigned" in out.err
    assert device.cc.writes == DEFAULT_WRITES


def test_cli_reports_an_absent_map(device, capsys):
    """Firmware without ``modulation cc-map`` is an error."""
    device.on("modulation cc-map", "{}")
    assert cli.main(["cc-map"]) == 1
    assert "no 12-slot CC map" in capsys.readouterr().err


def test_cli_probe_reports_the_probe(monkeypatch, capsys):
    """``--probe`` opens both links and reports ``midi.cc_probe``."""
    monkeypatch.setattr(cli.Videomancer, "open", lambda **_: nullcontext(object()))
    moved = tuple((p,) for p in range(1, PARAM_COUNT + 1))
    monkeypatch.setattr(cli, "probe_cc_map", lambda device, report: CcProbeReport(moved, True))
    assert cli.main(["cc-map", "--probe"]) == 0
    assert json.loads(capsys.readouterr().out)["probe"]["ok"]


def test_sink_cc_avoids_every_sent_controller():
    """The sink is the first undefined controller the map leaves free, and none free is an error."""
    assert sink_cc() == 119
    midi = MidiController(None, cc_map=[119, 118] + list(range(10)))
    assert sink_cc(midi) == 117
    crowded = MidiController(None, cc_map=list(range(108, 120)), high_resolution=False)
    assert sink_cc(crowded) == 107
    with pytest.raises(ValueError, match="none is free"):
        sink_cc_of([(cc, None) for cc in ASSIGN_SINK_CCS])


def test_reassert_repairs_only_a_sink_capture():
    """A slot on the sink CC is reassigned and reported armed; a correct map is left alone."""
    device = FakeCcMap()
    assert reassert_cc_map(device) is None and not device.writes
    device.live[9] = 119
    assigned = reassert_cc_map(device)
    assert assigned.before.armed == (10,) and device.live == list(range(PARAM_COUNT))
    assert assigned.summary().endswith("(was: P10 CC119); MIDI Assign was armed on P10")
    assert json.loads(json.dumps(assigned.as_dict()))["before"]["armed"] == [10]


def test_reassert_raises_on_any_other_change():
    """A captured driven CC alongside a sink capture still raises, without writing."""
    device = FakeCcMap()
    device.live[9], device.live[11] = 119, 0
    with pytest.raises(CcMapError, match="changed"):
        reassert_cc_map(device)
    assert not device.writes


def test_control_change_sends_one_raw_cc(midi, midi_transport):
    """The raw CC path sends a single 7-bit message and validates the controller."""
    midi.control_change(119)
    assert midi_transport.data == bytes([0xB0, 119, 0])
    with pytest.raises(ValueError, match="cc must be"):
        midi.control_change(128)


def test_cli_load_assigns_the_cc_map(monkeypatch, capsys):
    """``vmancer load`` goes through the facade, so the map is assigned and logged."""
    transport = FakeByteTransport()
    transport.on_error("programs authors", ShellErrorCode.UNKNOWN_COMMAND, "unknown command")
    transport.on("program info", json.dumps({"name": "Passthru", "parameters": []}))
    monkeypatch.setattr("pyvmancer.shell.time.sleep", lambda _: None)
    device = Videomancer(midi=MidiController(FakeMidiTransport()), shell=ShellClient(transport))
    monkeypatch.setattr(cli.Videomancer, "open", lambda **_: device)
    assert cli.main(["load", "Passthru"]) == 0
    assert "cc-map: assigned P1..P12 -> CC0..11 (was: ok)" in capsys.readouterr().err
    assert len(transport.cc.writes) == 24
