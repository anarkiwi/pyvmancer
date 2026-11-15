"""MIDI latency measurement against a simulated device with frame-quantised, rate-capped CC application."""

import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

from pyvmancer.const import PARAM_COUNT, PARAM_MAX
from pyvmancer.diagnose import diagnose_latency, diagnose_midi
from pyvmancer.errors import VmancerError
from pyvmancer.latency import LatencyReport, RateResult, measure_latency
from pyvmancer.video import VideoStatus, frame_period

FRAME = 1 / 30


class FakeRig:
    """A device applying each CC at the first frame after ``delay``, at most ``cap`` messages per second."""

    def __init__(self, delay=0.0, cap=None, rtt=0.002, timing="1080p30", manual=0, offset=0, sources=None):
        self.now, self.delay, self.cap, self.rtt = 0.0, delay, cap, rtt
        self.sources = sources or [0] * PARAM_COUNT
        self.period = frame_period(timing) or FRAME
        self.status = VideoStatus(timing, "hdmi", True, raw={"output": {"timing": timing}})
        self.manual = np.full(PARAM_COUNT, manual, dtype=np.int64)
        self.offsets = np.full(PARAM_COUNT, offset, dtype=np.int64)
        self.midi = SimpleNamespace(high_resolution=True)
        self.queue, self.free, self.sent = [], 0.0, 0

    @property
    def shell(self):
        """The serial side is the same object."""
        return self

    def clock(self):
        """Simulated monotonic time."""
        return self.now

    def sleep(self, seconds):
        """Advance simulated time."""
        self.now += max(0.0, seconds)

    def set_params(self, values):
        """Queue one 14-bit CC pair per parameter behind any throughput backlog."""
        for param, value in values.items():
            for _ in range(2):
                self.free = max(self.now, self.free) + (1 / self.cap if self.cap else 0.0)
                self.sent += 1
            ready = self.free + self.delay
            self.queue.append((math.ceil(ready / self.period) * self.period, param - 1, value))

    def combined_values(self):
        """Apply every CC whose frame has come, then report ``manual + offset``."""
        self.now += self.rtt
        due = [entry for entry in self.queue if entry[0] <= self.now]
        self.queue = [entry for entry in self.queue if entry[0] > self.now]
        for _, index, value in due:
            self.offsets[index] = value
        return np.clip(self.manual + self.offsets, 0, PARAM_MAX)

    def program_state(self):
        """Stored manual values and modulation sources."""
        return {"m": self.manual.tolist(), "sr": self.sources}

    def manual_values(self, state):
        """Manual values from a ``program state`` reply."""
        return np.asarray(state["m"])

    def video_state(self):
        """Output timing as ``video status`` reports it."""
        return self.status


def _measure(rig, **kwargs):
    return measure_latency(rig, clock=rig.clock, sleep=rig.sleep, **{"seconds": 1.0, **kwargs})


def _checks(findings):
    return {f.check: f for f in findings}


def test_prompt_device_passes_and_is_restored():
    """A one-frame device passes both checks and gets its prior CC offset back."""
    rig = FakeRig(manual=100, offset=200)
    notes = []
    latency = _measure(rig, report=notes.append)
    assert latency.restored and latency.prior == (300,)
    assert rig.combined_values()[0] == 300 and rig.manual[0] == 100
    assert FRAME / 2 < latency.single_median <= FRAME + 2 * rig.rtt
    assert latency.readback == pytest.approx(rig.rtt)
    assert [r.rate for r in latency.rates] == [5, 10, 20, 40, 80]
    assert all(f.ok for f in diagnose_latency(latency))
    assert len(notes) == 6


def test_throughput_cap_is_a_backlog_naming_the_rate_that_kept_up():
    """A message-rate cap backs up the fastest ramp and the fix names the fastest rate that kept up."""
    rig = FakeRig(cap=100.0)
    latency = _measure(rig)
    lags = [r.lag for r in latency.rates]
    assert lags[-1] > lags[0] + FRAME
    checks = _checks(diagnose_latency(latency))
    assert checks["midi.latency"].ok
    assert not checks["midi.backlog"].ok
    assert "80/s" in checks["midi.backlog"].detail
    assert "send at most 40 sets/s" in checks["midi.backlog"].fix


def test_backlog_scales_with_parameter_count():
    """The same cap holds one parameter but backs up six, which send six times the messages."""
    rig = FakeRig(cap=500.0)
    one = _checks(diagnose_latency(_measure(rig)))
    six = _checks(diagnose_latency(_measure(FakeRig(cap=500.0), params=(1, 2, 3, 4, 5, 6))))
    assert one["midi.backlog"].ok
    assert not six["midi.backlog"].ok and "at most 40 sets/s" in six["midi.backlog"].fix


def test_backlog_at_every_rate():
    """A cap below every rate tried says so rather than naming a rate."""
    checks = _checks(diagnose_latency(_measure(FakeRig(cap=5.0), rates=(10, 20))))
    assert "even at 10 sets/s" in checks["midi.backlog"].fix


def test_unconverged_sentinel_stops_the_sweep():
    """A sentinel that never converges is reported and skips the faster rates."""
    latency = _measure(FakeRig(cap=20.0), rates=(5, 40, 80), timeout=0.5)
    assert [r.rate for r in latency.rates] == [5, 40]
    assert latency.rates[-1].lag is None
    assert "no convergence" in _checks(diagnose_latency(latency))["midi.backlog"].detail


def test_late_device_fails_latency_in_frames():
    """Applying writes four frames late fails ``midi.latency``."""
    latency = _measure(FakeRig(delay=4 * FRAME), rates=(5,))
    check = _checks(diagnose_latency(latency))["midi.latency"]
    assert not check.ok
    assert latency.frames(latency.single_median) > 4
    assert "frames" in check.detail and check.fix


def test_threshold_follows_the_output_timing():
    """The same delay is two PAL frames but five 720p60 frames."""
    delay = 1.5 / 25
    pal = _checks(diagnose_latency(_measure(FakeRig(delay=delay, timing="PAL"), rates=())))
    fast = _checks(diagnose_latency(_measure(FakeRig(delay=delay, timing="720p60"), rates=())))
    assert pal["midi.latency"].ok and not fast["midi.latency"].ok
    assert "midi.backlog" not in pal


def test_unknown_timing_skips_findings():
    """Without a frame period there is no threshold, so nothing is reported."""
    assert not diagnose_latency(_measure(FakeRig(timing="mystery"), rates=(5,)))


def test_modulated_parameter_is_refused_before_any_write():
    """A parameter with a modulation source is refused before any CC is sent."""
    rig = FakeRig(manual=100, offset=50, sources=[0, 1] + [0] * (PARAM_COUNT - 2))
    with pytest.raises(VmancerError, match="modulation sources"):
        _measure(rig, params=(1, 2))
    assert rig.sent == 0


def test_unreachable_midi_raises_and_still_restores():
    """CCs that never land raise, and the prior offset is still written back."""
    rig = FakeRig(manual=100, offset=50)
    writes = []
    rig.set_params = writes.append
    with pytest.raises(VmancerError, match="never reached"):
        _measure(rig, timeout=0.2)
    assert writes[-1] == {1: 50}


def test_unconverged_single_write_fails_latency():
    """A single write that never converged has no median and fails."""
    latency = LatencyReport((1,), "1080p30", FRAME, 0.002, np.array([0.03, np.nan]), (), (0,), True)
    assert latency.single_median is None and latency.single_max is None
    checks = _checks(diagnose_latency(latency))
    assert not checks["midi.latency"].ok and "midi.backlog" not in checks
    assert latency.as_dict()["single"]["samples_ms"] == [30.0, None]


def test_refuses_toggles_and_parked_high_manuals():
    """Toggles and manual values with no CC headroom are refused."""
    with pytest.raises(ValueError, match="toggles"):
        _measure(FakeRig(), params=(7,))
    with pytest.raises(VmancerError, match="park first"):
        _measure(FakeRig(manual=PARAM_MAX - 8))


def test_report_is_json_in_ms_and_frames():
    """The report serialises to JSON with every duration in ms and frames."""
    data = json.loads(json.dumps(_measure(FakeRig(), rates=(10,), samples=4).as_dict()))
    assert data["timing"] == "1080p30" and data["frame_ms"] == pytest.approx(33.333)
    assert data["single"]["median"]["frames"] == pytest.approx(
        data["single"]["median"]["ms"] / 33.333, rel=1e-3
    )
    assert len(data["single"]["samples_ms"]) == 4
    assert data["rates"][0]["sets"] == 10 and data["rates"][0]["messages_per_s"] > 0
    assert data["restored"]


def test_zero_elapsed_rate_has_no_rates_per_second():
    """Degenerate timings serialise as nulls rather than dividing by zero."""
    latency = LatencyReport(
        (1,), None, None, 0.0, np.array([0.0]), (RateResult(5.0, 1, 2, 0.0, 0.0),), (0,), True
    )
    row = latency.as_dict()["rates"][0]
    assert row["sent_rate"] is None and row["messages_per_s"] is None
    assert latency.as_dict()["frame_ms"] is None and latency.frames(1.0) is None


def test_diagnose_midi_measures_then_diagnoses():
    """``diagnose_midi`` measures and returns both MIDI findings."""
    rig = FakeRig()
    findings = diagnose_midi(rig, clock=rig.clock, sleep=rig.sleep, rates=(5,), seconds=0.5)
    assert [f.check for f in findings] == ["midi.latency", "midi.backlog"]


def test_manual_values_read_program_state(shell, byte_transport):
    """Manual values come from ``program state``, never the combined output."""
    byte_transport.on("program state", json.dumps({"m": list(range(PARAM_COUNT)), "t": [512] * PARAM_COUNT}))
    assert shell.manual_values().tolist() == list(range(PARAM_COUNT))
    assert "modulation status" not in byte_transport.written


def test_manual_values_raise_when_absent(shell, byte_transport):
    """A ``program state`` without ``m`` is an error."""
    byte_transport.on("program state", "{}")
    with pytest.raises(VmancerError, match="no 12-slot manual values"):
        shell.manual_values()
