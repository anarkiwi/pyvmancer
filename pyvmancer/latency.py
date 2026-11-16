"""MIDI CC write to serial readback latency, paced-write backlog, and per-slot CC addressing.

A CC is an offset on the manual value, so each target is ``manual + offset`` and
the prior offset, ``combined - manual``, is written back afterwards. Parameters
with a modulation source are refused, since their combined value is not theirs to set.
"""

import time
from typing import NamedTuple

import numpy as np

from .const import PARAM_COUNT, PARAM_MAX, ParamKind, param_kind
from .device import PARK_TOLERANCE
from .errors import VmancerError
from .video import frame_period

#: Every parameter, as probed by :func:`probe_cc_map`.
CC_PARAMS = tuple(range(1, PARAM_COUNT + 1))
#: Rates tried by default, in parameter sets per second.
DEFAULT_RATES = (5, 10, 20, 40, 80)


class RateResult(NamedTuple):
    """One paced ramp: ``sets`` writes of every parameter, then a sentinel ``lag`` seconds to converge."""

    rate: float
    sets: int
    messages: int
    elapsed: float
    lag: float | None


class LatencyReport(NamedTuple):
    """Readback round trip, single-write latency samples and per-rate sentinel lag, in seconds."""

    params: tuple
    timing: str | None
    frame_period: float | None
    readback: float
    single: np.ndarray
    rates: tuple
    prior: tuple
    restored: bool

    @property
    def single_median(self):
        """Median single-write latency, or ``None`` when a write never converged."""
        return None if np.isnan(self.single).any() else float(np.median(self.single))

    @property
    def single_max(self):
        """Worst single-write latency, or ``None`` when a write never converged."""
        return None if np.isnan(self.single).any() else float(np.max(self.single))

    def frames(self, seconds):
        """``seconds`` in frame periods, or ``None`` when either is unknown."""
        return None if seconds is None or not self.frame_period else seconds / self.frame_period

    def timed(self, seconds):
        """``{"ms", "frames"}`` for a duration in seconds."""
        frames = self.frames(seconds)
        return {
            "ms": None if seconds is None else round(seconds * 1e3, 2),
            "frames": None if frames is None else round(frames, 3),
        }

    def as_dict(self):
        """JSON-ready summary in milliseconds and frames."""
        return {
            "params": list(self.params),
            "timing": self.timing,
            "frame_ms": None if self.frame_period is None else round(self.frame_period * 1e3, 3),
            "readback": self.timed(self.readback),
            "single": {
                "median": self.timed(self.single_median),
                "max": self.timed(self.single_max),
                "samples_ms": [None if np.isnan(s) else round(s * 1e3, 2) for s in self.single],
            },
            "rates": [
                {
                    "rate": r.rate,
                    "sets": r.sets,
                    "sent_rate": round(r.sets / r.elapsed, 2) if r.elapsed > 0 else None,
                    "messages_per_s": round(r.messages / r.elapsed, 1) if r.elapsed > 0 else None,
                    "lag": self.timed(r.lag),
                }
                for r in self.rates
            ],
            "prior": list(self.prior),
            "restored": self.restored,
        }


class _Probe:
    """Writes CC offsets to unmodulated parameters and times their readback convergence.

    With ``floor`` the manual values are lowered to zero for full CC headroom, and
    :meth:`restore` puts them back along with the prior offsets.
    """

    def __init__(self, device, params, timeout, clock, sleep, floor=False):
        self.device, self.timeout, self.clock, self.sleep = device, timeout, clock, sleep
        self.params = tuple(params)
        self.index = np.array(self.params) - 1
        self.per_param = 2 if device.midi.high_resolution else 1
        self.reads = []
        state = device.shell.program_state()
        sources = np.asarray(state.get("sr", np.zeros(PARAM_COUNT)))[self.index]
        if np.any(sources != 0):
            raise VmancerError(
                f"P{list(params)} have modulation sources {sources.tolist()}; disable them first"
            )
        self.original = device.shell.manual_values(state)[self.index]
        self.manual = np.zeros_like(self.original) if floor else self.original.copy()
        self.headroom = PARAM_MAX - self.manual
        if np.any(self.headroom <= 2 * PARK_TOLERANCE):
            raise VmancerError(f"manual values {self.manual.tolist()} leave no CC headroom; park first")
        self.prior = self.read()
        self.offsets = np.clip(self.prior - self.original, 0, PARAM_MAX)
        self.held = self.original
        self._set_manual(self.manual)

    def _set_manual(self, values):
        """Write each probed slot's manual value where it differs from the one it holds."""
        for index in np.flatnonzero(values != self.held):
            self.device.shell.set_modulation(int(self.index[index]), int(values[index]))
        self.held = values

    def read(self):
        """Combined values of the probed parameters, recording the round trip."""
        start = self.clock()
        values = self.device.shell.combined_values()
        self.reads.append(self.clock() - start)
        return np.asarray(values, dtype=np.int64)[self.index]

    def write(self, offsets):
        """Send one CC offset per probed parameter; returns the send time."""
        sent = self.clock()
        values = np.broadcast_to(offsets, self.index.shape)
        self.device.set_params(dict(zip(self.params, (int(v) for v in values))))
        return sent

    def converge(self, target, since):
        """Seconds from ``since`` until every readback is within tolerance of ``target``, or ``None``."""
        while True:
            done = np.all(np.abs(self.read() - target) <= PARK_TOLERANCE)
            elapsed = self.clock() - since
            if done:
                return elapsed
            if elapsed > self.timeout:
                return None

    def reset(self):
        """Zero the offsets and wait for the manual values, or raise."""
        self.write(0)
        if self.converge(self.manual, self.clock()) is None:
            raise VmancerError(
                f"P{list(self.params)} readback never reached {self.manual.tolist()}; "
                "check the MIDI link reaches this device"
            )

    def singles(self, samples):
        """Latency of ``samples`` writes alternating full headroom and zero; NaN where never converged."""
        lags = []
        for k in range(samples):
            offsets = self.headroom * (1 - k % 2)
            lags.append(self.converge(self.manual + offsets, self.write(offsets)))
        return np.array([np.nan if lag is None else lag for lag in lags], dtype=np.float64)

    def ramp(self, rate, seconds):
        """Paced ramp across the lower half of the headroom, then a full-headroom sentinel."""
        sets = max(1, int(round(rate * seconds)))
        start = self.clock()
        for k, fraction in enumerate(np.linspace(0.0, 0.5, sets)):
            self.sleep(max(0.0, start + k / rate - self.clock()))
            self.write(np.rint(self.headroom * fraction))
        sent = self.write(self.headroom)
        lag = self.converge(self.manual + self.headroom, sent)
        return RateResult(float(rate), sets, sets * len(self.params) * self.per_param, sent - start, lag)

    def restore(self):
        """Write back the prior offsets and manual values; True once the prior combined values read back."""
        self.write(self.offsets)
        self._set_manual(self.original)
        return self.converge(self.prior, self.clock()) is not None


def measure_latency(
    device,
    *,
    params=(1,),
    rates=DEFAULT_RATES,
    seconds=2.0,
    samples=10,
    timeout=5.0,
    clock=time.monotonic,
    sleep=time.sleep,
    report=None,
) -> LatencyReport:
    """Measure CC write to readback latency and per-rate backlog on a :class:`Videomancer`.

    Single writes alternate offset 0 and full headroom ``samples`` times; each rate
    ramps for ``seconds`` then times a sentinel, ``None`` if not converged within
    ``timeout``, which also ends the sweep. Touched parameters are then restored.
    """
    if any(param_kind(p) is ParamKind.TOGGLE for p in params):
        raise ValueError(f"toggles cannot carry a ramp; choose knobs or the fader, got {list(params)}")
    report = report or (lambda _message: None)
    timing = device.shell.video_state().output_timing
    probe = _Probe(device, params, timeout, clock, sleep)
    single, results = np.empty(0), []
    try:
        probe.reset()
        report(f"single writes x{samples} on P{list(params)}")
        single = probe.singles(samples)
        probe.reset()
        for rate in sorted(rates):
            report(f"ramp at {rate:g} sets/s for {seconds:g} s")
            results.append(probe.ramp(rate, seconds))
            if results[-1].lag is None:
                break
            probe.reset()
    finally:
        restored = probe.restore()
    return LatencyReport(
        params=probe.params,
        timing=timing,
        frame_period=frame_period(timing),
        readback=float(np.median(probe.reads)),
        single=single,
        rates=tuple(results),
        prior=tuple(int(v) for v in probe.prior),
        restored=restored,
    )


class CcProbeReport(NamedTuple):
    """Per param, the params whose readback moved when only its CC was written."""

    moved: tuple
    restored: bool

    @property
    def ok(self):
        """True when each CC moved its own slot and nothing else."""
        return all(moved == (param,) for param, moved in zip(CC_PARAMS, self.moved))

    def as_dict(self):
        """JSON-ready summary."""
        moved = {str(p): list(m) for p, m in zip(CC_PARAMS, self.moved)}
        return {"ok": self.ok, "moved": moved, "restored": self.restored}


def probe_cc_map(
    device, *, timeout=1.0, clock=time.monotonic, sleep=time.sleep, report=None
) -> CcProbeReport:
    """Write each param's CC alone over MIDI and record which slots read back moved.

    Manual values are lowered to zero for headroom; they and the prior CC offsets
    are restored afterwards. Every slot must be free of modulation sources.
    """
    report = report or (lambda _message: None)
    probe = _Probe(device, CC_PARAMS, timeout, clock, sleep, floor=True)
    moved = []
    try:
        for param, headroom in zip(CC_PARAMS, probe.headroom):
            probe.reset()
            report(f"P{param} alone at offset {headroom}")
            target = probe.manual.copy()
            target[param - 1] += headroom
            device.set_params({param: int(headroom)})
            probe.converge(target, clock())
            delta = np.abs(probe.read() - probe.manual) > PARK_TOLERANCE
            moved.append(tuple(int(i) + 1 for i in np.flatnonzero(delta)))
    finally:
        restored = probe.restore()
    return CcProbeReport(tuple(moved), restored)
