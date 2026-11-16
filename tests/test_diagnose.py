"""Diagnostic findings over real ``video status`` payloads from firmware 1.0.1."""

import json

import pytest

from pyvmancer.const import DEFAULT_CC_MSB, ROUTE_HDMI_IN
from pyvmancer.diagnose import Finding, diagnose, diagnose_route, diagnose_video, format_finding, report
from pyvmancer.video import VideoStatus

from .conftest import FakeCcMap

GOOD = {
    "source": "hdmi",
    "timing": "1080p30",
    "locked": True,
    "overridden": False,
    "external_vsync": True,
    "analog": {"locked": False, "timing": "NTSC"},
    "hdmi": {"locked": True, "connected": False, "timing": "1080p30"},
    "output": {"timing": "1080p30", "hdmi_connected": True, "analog_configured": True, "sfl": False},
    "enc": {"r80": "10", "r84": "00", "r8b": "00", "fsc": "21F07C1F"},
    "hdmi_tx": {
        "r17": "03",
        "de": "2FE90F",
        "r3b": "00",
        "vic": 34,
        "vic_rx": 16,
        "vic_det": "40",
        "r3f": "00",
        "r42": "F0",
        "r9e": "12",
    },
}

HELD = {
    "source": "hdmi",
    "timing": "NTSC",
    "locked": True,
    "overridden": True,
    "external_vsync": False,
    "analog": {"locked": True, "timing": "NTSC"},
    "hdmi": {"locked": True, "connected": False, "timing": "1080p30"},
    "output": {"timing": "NTSC", "hdmi_connected": True, "analog_configured": True, "sfl": False},
    "enc": {"r80": "10", "r84": "00", "r8b": "80", "fsc": "21F07C1F"},
    "hdmi_tx": {
        "r17": "01",
        "de": "1D9205",
        "r3b": "4A",
        "vic": 6,
        "vic_rx": 70,
        "vic_det": "40",
        "r3f": "00",
        "r42": "F0",
        "r9e": "16",
    },
}

DEAD_TX = dict(
    HELD,
    hdmi_tx={
        "r17": "00",
        "de": "000000",
        "r3b": "80",
        "vic": 0,
        "vic_rx": 16,
        "vic_det": "40",
        "r3f": "00",
        "r42": "F0",
        "r9e": "01",
    },
)


def _by_check(data):
    """Findings for a payload, keyed by check id."""
    return {f.check: f for f in diagnose_video(VideoStatus.from_json(data))}


def test_genlocked_device_passes_every_check():
    """A genlocked HDMI passthrough raises nothing, with concrete values in the details."""
    findings = _by_check(GOOD)
    assert list(findings) == [
        "video.input_locked",
        "video.genlock",
        "video.output_active",
        "video.output_connected",
    ]
    assert all(f.ok and not f.fix for f in findings.values())
    assert findings["video.input_locked"].detail == "hdmi input locked at 1080p30"
    assert findings["video.output_active"].detail == "hdmi transmitter vic 34, de 2FE90F"


def test_route_held_output_names_the_front_panel():
    """Output held at NTSC under a locked 1080p30 input is a route fault, not a source fault."""
    findings = _by_check(HELD)
    genlock = findings["video.genlock"]
    assert not genlock.ok
    assert genlock.detail == "output held at NTSC while hdmi input is locked at 1080p30"
    assert "Vid Route Mode" in genlock.fix and "Pref. Timing to 1080p30" in genlock.fix
    assert "power-cycle" in genlock.fix and "`video timing`" in genlock.fix
    assert findings["video.input_locked"].ok
    assert findings["video.output_active"].ok


def test_dead_transmitter_is_named():
    """A transmitter with no VIC and no data enable is driving no video."""
    active = _by_check(DEAD_TX)["video.output_active"]
    assert not active.ok
    assert active.detail == "hdmi transmitter vic 0, de 000000"
    assert "vmancer resync" in active.fix


def test_lost_source_explains_the_override():
    """With the input unlocked, the override is reported as a consequence of no source."""
    findings = _by_check(dict(DEAD_TX, hdmi=dict(DEAD_TX["hdmi"], locked=False)))
    assert not findings["video.input_locked"].ok
    assert findings["video.input_locked"].fix.startswith("the source on hdmi is absent")
    genlock = findings["video.genlock"]
    assert not genlock.ok and "no signal" in genlock.detail and "Vid Route" not in genlock.fix


def test_forced_timing_matching_the_input_is_fine():
    """An override that agrees with the input's timing is not a fault."""
    genlock = _by_check(dict(GOOD, overridden=True))["video.genlock"]
    assert genlock.ok and genlock.detail == "output forced to 1080p30, matching hdmi input"


def test_advisory_connected_flag_is_never_failed():
    """``hdmi.connected`` reads false while passing video, so it carries no finding."""
    assert GOOD["hdmi"]["connected"] is False
    assert report(_by_check(GOOD).values())["ok"]


def test_missing_sink():
    """No HDMI sink fails the connection check and skips the activity check."""
    findings = _by_check(dict(GOOD, output=dict(GOOD["output"], hdmi_connected=False)))
    assert not findings["video.output_connected"].ok
    assert "video.output_active" not in findings


@pytest.mark.parametrize(
    "data,expected",
    [
        ({}, []),
        ({"source": "hdmi", "overridden": True, "hdmi": {"locked": True}}, ["video.input_locked"]),
        ({"locked": False}, ["video.input_locked"]),
        ({"output": {"hdmi_connected": True}}, ["video.output_connected"]),
        ({"output": "garbage", "hdmi_tx": {"vic": 1}}, []),
    ],
)
def test_checks_without_inputs_are_skipped(data, expected):
    """Firmware drift that removes a check's inputs skips the check rather than failing it."""
    assert list(_by_check(data)) == expected


class FakeShell(FakeCcMap):
    """The reads ``diagnose`` makes, answered from fixtures."""

    def __init__(self, fpga, video, live=DEFAULT_CC_MSB, route=ROUTE_HDMI_IN):
        super().__init__(live, route=route)
        self.fpga, self.video = fpga, video

    def version(self):
        """Firmware version with the trailing newline the device sends."""
        return "1.0.1\n"

    def fpga_status(self):
        """Scripted ``fpga status``."""
        return self.fpga

    def video_state(self):
        """Scripted ``video status``."""
        return VideoStatus.from_json(self.video)


RUNNING = {"state": "running", "configured": True, "program": "Passthru"}


def test_diagnose_runs_device_checks_first():
    """Firmware and FPGA findings precede the video checks."""
    findings = diagnose(FakeShell(RUNNING, GOOD))
    assert findings[:2] == [
        Finding("firmware.version", True, "1.0.1"),
        Finding("fpga.running", True, "state running, configured True, program Passthru"),
    ]
    assert findings[2].check == "midi.cc_map" and findings[2].ok
    assert findings[3] == Finding("video.route", True, "Vid Route Mode 1 (HDMI In)")
    assert len(findings) == 8


@pytest.mark.parametrize(
    "fpga", [{"state": "running", "configured": False}, {"state": "idle", "configured": True}]
)
def test_fpga_not_running(fpga):
    """Both the state and the configured flag must hold."""
    finding = diagnose(FakeShell(fpga, {}))[1]
    assert not finding.ok and "vmancer load" in finding.fix


def test_fpga_without_state_is_skipped():
    """An ``fpga status`` lacking ``state`` contributes no finding."""
    assert [f.check for f in diagnose(FakeShell({}, {}))] == [
        "firmware.version",
        "midi.cc_map",
        "video.route",
    ]


def test_report_and_format():
    """The report is JSON-ready and the human lines carry the fix only on failure."""
    findings = diagnose_video(VideoStatus.from_json(HELD))
    result = report(iter(findings))
    assert not result["ok"]
    assert json.loads(json.dumps(result))["findings"][1]["check"] == "video.genlock"
    assert format_finding(findings[0]) == "ok   video.input_locked: hdmi input locked at 1080p30"
    assert format_finding(findings[1]).startswith(
        "FAIL video.genlock: output held at NTSC while hdmi input is locked at 1080p30 -> on the front panel"
    )
    assert report([]) == {"ok": True, "findings": []}


def _route(shell, expected):
    """The ``video.route`` finding of a full diagnosis."""
    return {f.check: f for f in diagnose(shell, route=expected)}.get("video.route")


def test_route_matching_expected_passes():
    """Vid Route Mode reading the expected value passes, naming the observed mode."""
    finding = _route(FakeShell(RUNNING, GOOD), ROUTE_HDMI_IN)
    assert finding == Finding("video.route", True, "Vid Route Mode 1 (HDMI In), expected 1 (HDMI In)")


def test_route_mismatch_fails_while_video_status_reads_hdmi_locked():
    """Under another route the hdmi input still reads locked, so only the route finding fails."""
    findings = diagnose(FakeShell(RUNNING, GOOD, route=4), route=ROUTE_HDMI_IN)
    failed = [f for f in findings if not f.ok]
    assert [f.check for f in failed] == ["video.route"]
    assert failed[0].detail == "Vid Route Mode 4 (Auto), expected 1 (HDMI In)"
    assert "Vid Route Mode to HDMI In" in failed[0].fix


def test_unconfirmed_route_values_are_shown_by_number():
    """A value never observed is not named, in the reading or the fix."""
    finding = diagnose_route(3, 7)
    assert finding.detail == "Vid Route Mode 3 (unconfirmed mode), expected 7 (unconfirmed mode)"
    assert "the mode that reads 7" in finding.fix


def test_unreported_route_skips_unless_expected():
    """A firmware without the key reports nothing, unless a route is expected."""
    assert _route(FakeShell(RUNNING, GOOD, route=None), None) is None
    finding = _route(FakeShell(RUNNING, GOOD, route=None), ROUTE_HDMI_IN)
    assert not finding.ok and finding.detail == "Vid Route Mode not reported, expected 1 (HDMI In)"
