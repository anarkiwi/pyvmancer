"""Device diagnostics: named checks reporting what was observed and how to fix it.

Each check yields a :class:`Finding`. A check whose inputs the firmware did not
report is skipped rather than failed, so key drift between releases never
manufactures a fault. ``hdmi.connected`` is advisory and never checked. :func:`diagnose` only reads;
:func:`diagnose_midi` writes parameters and restores them.
"""

from typing import Iterable, NamedTuple

from .ccmap import CcMapReport, cc_map_report, expected_cc_map
from .latency import CcProbeReport, LatencyReport, measure_latency
from .video import VideoStatus

#: The CLI command that restores the default CC map.
RESTORE_COMMAND = "vmancer cc-map --restore --yes"


class Finding(NamedTuple):
    """Outcome of one diagnostic check."""

    check: str
    ok: bool
    detail: str
    fix: str = ""


def _finding(check, ok, detail, fix):
    """A :class:`Finding` whose ``fix`` is kept only when the check failed."""
    return Finding(check, bool(ok), detail, "" if ok else fix)


def _input_locked(status, source):
    """``video.input_locked``: the selected input's own sub-status reports lock."""
    sub = status.raw.get(str(source))
    if not (isinstance(sub, dict) and "locked" in sub) and "locked" not in status.raw:
        return None
    locked = status.source_locked
    timing = f" at {status.source_timing}" if locked and status.source_timing else ""
    return _finding(
        "video.input_locked",
        locked,
        f"{source} input {'locked' + timing if locked else 'reports no lock'}",
        f"the source on {source} is absent or powered down; check it is outputting and the cable",
    )


def _genlock(status, source):
    """``video.genlock``: the output follows the selected input, or is forced to its timing."""
    if "overridden" not in status.raw:
        return None
    output, wanted = status.output_timing, status.source_timing
    if not status.overridden:
        return Finding("video.genlock", True, f"output {output} follows {source} input")
    if not status.source_locked:
        return Finding(
            "video.genlock",
            False,
            f"output held at {output} because {source} input has no signal",
            f"restore the source on {source}; the override is a consequence of losing it",
        )
    if wanted is None:
        return None
    return _finding(
        "video.genlock",
        output == wanted,
        (
            f"output forced to {output}, matching {source} input"
            if output == wanted
            else f"output held at {output} while {source} input is locked at {wanted}"
        ),
        f"on the front panel set SETTINGS > Vid Route Mode to Auto or {source}, SYSTEM > Pref. Timing "
        f"to {wanted}, then power-cycle; `video timing` acknowledges but does not clear this",
    )


def _output_active(output, transmitter):
    """``video.output_active``: an attached HDMI sink is being driven with active video."""
    if not output.get("hdmi_connected") or not {"vic", "de"} <= transmitter.keys():
        return None
    vic, de = transmitter["vic"], str(transmitter["de"])
    return _finding(
        "video.output_active",
        vic != 0 and de.strip("0") != "",
        f"hdmi transmitter vic {vic}, de {de}",
        "the HDMI transmitter is not driving video; restore the source, or run `vmancer resync`",
    )


def _output_connected(output):
    """``video.output_connected``: a sink is attached to the HDMI output."""
    if "hdmi_connected" not in output:
        return None
    connected = output["hdmi_connected"]
    return _finding(
        "video.output_connected",
        connected,
        f"hdmi output sink {'attached' if connected else 'not detected'}",
        "attach a display or capture device to the HDMI output and check the cable",
    )


def _section(raw, key):
    """A nested ``video status`` object, or an empty one when absent."""
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def diagnose_video(status: VideoStatus) -> list[Finding]:
    """Video checks over a parsed ``video status``; pure, reads only ``status.raw``."""
    source = status.input_source or "selected"
    output, transmitter = _section(status.raw, "output"), _section(status.raw, "hdmi_tx")
    findings = (
        _input_locked(status, source),
        _genlock(status, source),
        _output_active(output, transmitter),
        _output_connected(output),
    )
    return [finding for finding in findings if finding is not None]


def _cc(slot):
    """``msb/lsb`` of a slot, ``-`` for an LSB that is not sent."""
    return f"{slot[0]}/{'-' if slot[1] is None else slot[1]}"


def cc_map_fix(cc_map: CcMapReport) -> str:
    """How to reassign ``cc_map``'s expected map; the CLI command covers the default one."""
    if cc_map.expected == expected_cc_map():
        return f"run `{RESTORE_COMMAND}`, which writes every slot's CC and reads the map back"
    return "call `pyvmancer.assign_cc_map(shell, midi)` with the controller that drives the device"


def diagnose_cc_map(cc_map: CcMapReport) -> Finding:
    """``midi.cc_map``: every slot listens on the CC pyvmancer sends it, and no CC drives two slots."""
    problems = [
        f"P{p} on {_cc(cc_map.actual[p - 1])}, want {_cc(cc_map.expected[p - 1])}" for p in cc_map.mismatched
    ]
    groups = sorted({(p, *o) for p, o in cc_map.shared.items() if p < min(o)})
    problems += [f"P{', P'.join(map(str, g))} share a CC" for g in groups]
    problems += [f"P{p} unassigned" for p in cc_map.unassigned]
    detail = "; ".join(problems) or (
        f"P1..P{len(cc_map.expected)} on msb/lsb " + " ".join(_cc(slot) for slot in cc_map.expected)
    )
    return _finding("midi.cc_map", cc_map.ok, detail, cc_map_fix(cc_map))


def diagnose_cc_probe(probe: CcProbeReport) -> Finding:
    """``midi.cc_probe``: writing one slot's CC over MIDI moved that slot and nothing else."""
    wrong = [
        f"P{p} CC moved {', '.join(f'P{m}' for m in moved) or 'nothing'}"
        for p, moved in enumerate(probe.moved, start=1)
        if moved != (p,)
    ]
    return _finding(
        "midi.cc_probe",
        probe.ok,
        "; ".join(wrong) or f"each of P1..P{len(probe.moved)} moved only itself",
        f"run `vmancer cc-map` to see the device map, then `{RESTORE_COMMAND}`",
    )


def diagnose(shell, midi=None) -> list[Finding]:
    """Run every read-only device check over an open :class:`~pyvmancer.shell.ShellClient`.

    ``midi`` is the :class:`~pyvmancer.midi.MidiController` whose CC map is expected; default CCs otherwise.
    """
    findings = [Finding("firmware.version", True, shell.version().strip())]
    fpga = shell.fpga_status()
    if "state" in fpga:
        running = fpga["state"] == "running" and bool(fpga.get("configured"))
        findings.append(
            _finding(
                "fpga.running",
                running,
                f"state {fpga['state']}, configured {fpga.get('configured')}, program {fpga.get('program')}",
                "no FPGA program is running; load one, e.g. `vmancer load Passthru`",
            )
        )
    cc_map = cc_map_report(shell, midi)
    if cc_map is not None:
        findings.append(diagnose_cc_map(cc_map))
    return findings + diagnose_video(shell.video_state())


def _timed(latency, seconds):
    """``<ms> ms (<frames> frames)`` for a duration in seconds."""
    if seconds is None:
        return "no convergence"
    return f"{seconds * 1e3:.1f} ms ({latency.frames(seconds):.2f} frames)"


def diagnose_latency(latency: LatencyReport) -> list[Finding]:
    """``midi.latency`` and ``midi.backlog`` over a measured report, thresholded in frame periods.

    A single write may take two frames plus the readback round trip; a paced
    ramp's sentinel may lag a single write by one frame. Unknown timing skips both.
    """
    frame, median = latency.frame_period, latency.single_median
    if not frame:
        return []
    limit = 2 * frame + latency.readback
    findings = [
        _finding(
            "midi.latency",
            median is not None and median <= limit,
            f"single write median {_timed(latency, median)}, max {_timed(latency, latency.single_max)}, "
            f"readback {_timed(latency, latency.readback)}, "
            f"limit {_timed(latency, limit)} at {latency.timing}",
            "the device applies CC writes late; any CC-driven timing will trail its cue by the median",
        )
    ]
    if median is None or not latency.rates:
        return findings
    kept = [r.lag is not None and r.lag <= median + frame for r in latency.rates]
    best = max((r.rate for r, ok in zip(latency.rates, kept) if ok), default=None)
    findings.append(
        _finding(
            "midi.backlog",
            all(kept),
            ", ".join(
                f"{r.rate:g}/s x{len(latency.params)} sentinel {_timed(latency, r.lag)}"
                for r in latency.rates
            ),
            (
                f"CC writes back up above {best:g} sets/s; send at most {best:g} sets/s"
                if best is not None
                else f"CC writes back up even at {latency.rates[0].rate:g} sets/s; "
                "send fewer sets, or fewer parameters per set"
            ),
        )
    )
    return findings


def diagnose_midi(device, **kwargs) -> list[Finding]:
    """Run :func:`~pyvmancer.latency.measure_latency` and diagnose it; writes, then restores, parameters."""
    return diagnose_latency(measure_latency(device, **kwargs))


def report(findings: Iterable[Finding]) -> dict:
    """JSON-ready summary: overall ``ok`` and every finding as a dict."""
    findings = list(findings)
    return {"ok": all(f.ok for f in findings), "findings": [f._asdict() for f in findings]}


def format_finding(finding: Finding) -> str:
    """One human-readable line, e.g. ``FAIL video.genlock: <detail> -> <fix>``."""
    fix = f" -> {finding.fix}" if finding.fix else ""
    return f"{'ok  ' if finding.ok else 'FAIL'} {finding.check}: {finding.detail}{fix}"
