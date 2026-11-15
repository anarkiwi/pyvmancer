"""Device diagnostics: named checks reporting what was observed and how to fix it.

Each check yields a :class:`Finding`. A check whose inputs the firmware did not
report is skipped rather than failed, so key drift between releases never
manufactures a fault. ``hdmi.connected`` is advisory and never checked.
"""

from typing import Iterable, NamedTuple

from .video import VideoStatus


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


def diagnose(shell) -> list[Finding]:
    """Run every device check over an open :class:`~pyvmancer.shell.ShellClient`."""
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
    return findings + diagnose_video(shell.video_state())


def report(findings: Iterable[Finding]) -> dict:
    """JSON-ready summary: overall ``ok`` and every finding as a dict."""
    findings = list(findings)
    return {"ok": all(f.ok for f in findings), "findings": [f._asdict() for f in findings]}


def format_finding(finding: Finding) -> str:
    """One human-readable line, e.g. ``FAIL video.genlock: <detail> -> <fix>``."""
    fix = f" -> {finding.fix}" if finding.fix else ""
    return f"{'ok  ' if finding.ok else 'FAIL'} {finding.check}: {finding.detail}{fix}"
