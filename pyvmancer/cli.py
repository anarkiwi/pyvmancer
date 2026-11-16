"""Command-line interface: ``vmancer <subcommand>``."""

import argparse
import json
import os
import sys

from . import __version__, firmware, library
from .ccmap import assign_cc_map, cc_map_report
from .diagnose import (
    diagnose,
    diagnose_cc_map,
    diagnose_cc_probe,
    diagnose_latency,
    diagnose_video,
    format_finding,
    report,
)
from .const import OPERATORS, PARAM_COUNT
from .device import Videomancer, open_midi, open_shell
from .discovery import find_devices
from .errors import VmancerError
from .latency import DEFAULT_RATES, measure_latency, probe_cc_map


def _param_value(text):
    """Parse a CLI parameter value as bool, float or device-unit int."""
    lowered = text.strip().lower()
    if lowered in ("on", "true", "yes"):
        return True
    if lowered in ("off", "false", "no"):
        return False
    if "." in lowered:
        return float(lowered)
    return int(lowered)


def _assignment(text):
    """Parse a ``P=V`` assignment into ``(param, value)``."""
    key, sep, value = text.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected PARAM=VALUE, got {text!r}")
    param = int(key)
    if not 1 <= param <= PARAM_COUNT:
        raise argparse.ArgumentTypeError(f"parameter must be 1..{PARAM_COUNT}, got {param}")
    return param, _param_value(value)


def _numbers(kind):
    """Parse a comma-separated list of ``kind``."""
    return lambda text: tuple(kind(part) for part in text.split(",") if part.strip())


def _print_json(data):
    """Emit a JSON document on stdout."""
    print(json.dumps(data, indent=2, default=str))


def build_parser():
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(prog="vmancer", description=__doc__)
    parser.add_argument("--version", action="version", version=f"pyvmancer {__version__}")
    parser.add_argument("--serial", dest="serial_number", help="target a specific device serial")
    parser.add_argument("--channel", type=int, help="MIDI channel 1-16 (default: omni/1)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list attached Videomancers and their device nodes")
    sub.add_parser("operators", help="list modulation operators and their ids")

    setter = sub.add_parser("set", help="set parameters, e.g. set 1=0.5 7=on")
    setter.add_argument("assignments", nargs="+", type=_assignment)

    trigger = sub.add_parser("trigger", help="send a note trigger to a parameter")
    trigger.add_argument("param", type=int)
    trigger.add_argument("--velocity", type=int, default=127)

    preset = sub.add_parser("preset", help="recall a preset by MIDI program change")
    preset.add_argument("program", type=int)

    clock = sub.add_parser("clock", help="emit MIDI clock at a tempo")
    clock.add_argument("bpm", type=float)
    clock.add_argument("--beats", type=float, default=4.0)

    sub.add_parser("status", help="query device status over serial")
    programs = sub.add_parser("programs", help="list installed FPGA programs over serial")
    programs.add_argument("--author", help="list only this author's programs (default: all, qualified)")
    sub.add_parser("authors", help="list program authors over serial")
    sub.add_parser("presets", help="list factory and user presets over serial")

    load = sub.add_parser("load", help="load an FPGA program by name or author/Name, then assign the CC map")
    load.add_argument("name")
    load.add_argument(
        "--stall",
        type=float,
        help="opt-in: fail once nothing observable changes for this many seconds (default: wait on USB)",
    )

    sub.add_parser("manifest", help="show the SD program manifest over serial")
    sub.add_parser("resync", help="bounce the video timing to recover the output")

    hasher = sub.add_parser("hash", help="device-computed sha256 of a file on the SD card")
    hasher.add_argument("path")

    doctor = sub.add_parser("doctor", help="diagnose the device; exits 1 if any check fails")
    doctor.add_argument(
        "--latency", action="store_true", help="also measure MIDI latency (writes, then restores)"
    )
    _add_latency_args(doctor)
    latency = sub.add_parser("latency", help="measure MIDI CC write to readback latency and backlog")
    _add_latency_args(latency)

    cc_map = sub.add_parser("cc-map", help="compare the device MIDI CC map with the CCs pyvmancer sends")
    mode = cc_map.add_mutually_exclusive_group()
    mode.add_argument(
        "--restore", action="store_true", help="write every slot's CC via settings keys and verify"
    )
    mode.add_argument(
        "--probe",
        action="store_true",
        help="write each CC over MIDI and check only its slot moves (restores)",
    )
    cc_map.add_argument("--yes", action="store_true", help="actually --restore; otherwise this is a dry run")

    shell_cmd = sub.add_parser("shell", help="run a raw serial shell command")
    shell_cmd.add_argument("--timeout", type=float, help="reply deadline in seconds (default: 5)")
    shell_cmd.add_argument("words", nargs="+")

    _add_video(sub)

    _add_firmware(sub)
    _add_library(sub)
    return parser


def _add_latency_args(parser):
    """Arguments shared by ``latency`` and ``doctor --latency``."""
    parser.add_argument(
        "--param", type=_numbers(int), default=(1,), help="parameters, e.g. 1 or 1,2,3 (default: 1)"
    )
    parser.add_argument(
        "--rates",
        type=_numbers(float),
        default=DEFAULT_RATES,
        help=f"parameter sets per second (default: {','.join(map(str, DEFAULT_RATES))})",
    )
    parser.add_argument("--seconds", type=float, default=2.0, help="ramp length per rate (default: 2)")


def _add_video(sub):
    """Add the ``video`` subcommand group."""
    group = sub.add_parser("video", help="inspect and set the video input and timing")
    actions = group.add_subparsers(dest="action", required=True)
    actions.add_parser("status", help="raw video status plus diagnostic findings")
    actions.add_parser("timings", help="timing standards the firmware accepts")
    timing = actions.add_parser("timing", help="set the output timing and confirm it took")
    timing.add_argument("standard")
    source = actions.add_parser("input", help="select the video input")
    source.add_argument("source")


def _add_library(sub):
    """Add the ``library`` subcommand group, covering the SD program library."""
    group = sub.add_parser("library", help="list and install the FPGA program library")
    actions = group.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="list published program library releases")
    listing.add_argument("--limit", type=int, default=10, help="releases to show (default: 10)")

    actions.add_parser("status", help="compare the card's programs against the newest release")

    fetch = actions.add_parser("download", help="download and verify an archive without installing")
    fetch.add_argument("version", nargs="?", default="latest", help="version, or 'latest' (default)")
    fetch.add_argument("--dir", dest="directory", help="download directory")

    install = actions.add_parser("install", help="install a program library release onto the card")
    install.add_argument("version", nargs="?", default="latest", help="version, or 'latest' (default)")
    install.add_argument("--yes", action="store_true", help="actually install; otherwise this is a dry run")
    install.add_argument("--force", action="store_true", help="re-upload files already present at size")
    install.add_argument("--dir", dest="directory", help="download directory")
    install.add_argument("--no-verify", action="store_true", help="skip the post-upload size check")


def _add_firmware(sub):
    """Add the ``firmware`` subcommand group."""
    group = sub.add_parser("firmware", help="list, download and flash device firmware")
    actions = group.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="list published firmware releases")
    listing.add_argument("--limit", type=int, default=10, help="releases to show (default: 10)")
    listing.add_argument("--notes", action="store_true", help="include release notes")

    actions.add_parser("status", help="compare the running firmware against the newest release")

    fetch = actions.add_parser("download", help="download a release's UF2 without flashing it")
    fetch.add_argument("version", nargs="?", default="latest", help="version, or 'latest' (default)")
    fetch.add_argument("--dir", dest="directory", help="download directory")

    up = actions.add_parser("upgrade", help="flash a release onto the device via the UF2 bootloader")
    up.add_argument("version", nargs="?", default="latest", help="version, or 'latest' (default)")
    up.add_argument("--yes", action="store_true", help="actually flash; otherwise this is a dry run")
    up.add_argument("--force", action="store_true", help="flash even if the device is already on it")
    up.add_argument("--stable", action="store_true", help="refuse prereleases when picking 'latest'")
    up.add_argument("--dir", dest="directory", help="download directory")
    up.add_argument("--volume", help="mount point of an already-mounted bootloader volume")
    up.add_argument(
        "--manual",
        action="store_true",
        help="device is already in bootloader mode; do not open the serial link",
    )


def _run_midi(args):
    """Handle subcommands that only need the MIDI link."""
    with open_midi(serial_number=args.serial_number, channel=args.channel) as midi:
        if args.command == "set":
            midi.set_params(dict(args.assignments))
            _print_json({"state": midi.state.tolist()})
        elif args.command == "trigger":
            midi.pulse(args.param, velocity=args.velocity)
        elif args.command == "preset":
            midi.select_preset(args.program)
        elif args.command == "clock":
            midi.run_clock(args.bpm, args.beats)
    return 0


def _run_shell(args):
    """Handle subcommands that need the serial link."""
    with open_shell(serial_number=args.serial_number) as shell:
        shell.report = _note
        if args.command == "status":
            _print_json(shell.status())
        elif args.command == "programs":
            _print_json(shell.programs(args.author))
        elif args.command == "authors":
            _print_json(shell.authors())
        elif args.command == "presets":
            _print_json(shell.presets())
        elif args.command == "manifest":
            manifest = shell.program_manifest()
            _print_json({"version": manifest.version, "programs": manifest.raw.get("programs", [])})
        elif args.command == "resync":
            _print_json({"locked": shell.resync()})
        elif args.command == "hash":
            _print_json(shell.hash_file(args.path))
        elif args.command == "shell":
            print(shell.command(*args.words, timeout=args.timeout).payload)
        elif args.command == "video":
            return _run_video(args, shell)
    return 0


def _report(findings, **extra):
    """Print a findings report as JSON on stdout and one line per finding on stderr."""
    result = report(findings)
    for finding in findings:
        print(format_finding(finding), file=sys.stderr)
    _print_json({**extra, **result})
    return int(not result["ok"])


def _run_device(args):
    """Handle ``doctor`` and ``latency``, which open both links when latency is measured."""
    if args.command == "doctor" and not args.latency:
        with open_shell(serial_number=args.serial_number) as shell:
            return _report(diagnose(shell))
    with Videomancer.open(serial_number=args.serial_number, channel=args.channel) as device:
        latency = measure_latency(
            device, params=args.param, rates=args.rates, seconds=args.seconds, report=_note
        )
        findings = diagnose_latency(latency)
        if args.command == "doctor":
            findings = diagnose(device.shell, device.midi) + findings
        return _report(findings, latency=latency.as_dict())


def _run_cc_map(args):
    """Handle ``cc-map``: compare, ``--restore`` (dry run unless ``--yes``) or ``--probe``."""
    if args.probe:
        with Videomancer.open(serial_number=args.serial_number, channel=args.channel) as device:
            probe = probe_cc_map(device, report=_note)
        return _report([diagnose_cc_probe(probe)], probe=probe.as_dict())
    with open_shell(serial_number=args.serial_number) as shell:
        if args.restore:
            assigned = assign_cc_map(shell, apply=args.yes)
            _note(assigned.summary())
            _print_json(
                {**assigned.as_dict(), **({} if args.yes else {"action": "re-run with --yes to write"})}
            )
            return 0
        found = cc_map_report(shell)
    if found is None:
        raise VmancerError("device reported no 12-slot CC map")
    return _report([diagnose_cc_map(found)], **found.as_dict())


def _run_load(args):
    """Load a program through the facade, so the CC map is assigned afterwards."""
    with Videomancer.open(serial_number=args.serial_number, channel=args.channel) as device:
        device.shell.report, device.shell.stall = _note, args.stall
        device.load_program(args.name)
        _note(device.cc_assignment.summary())
    return 0


def _run_video(args, shell):
    """Handle the ``video`` subcommand group."""
    if args.action == "status":
        state = shell.video_state()
        _report(diagnose_video(state), status=state.raw)
    elif args.action == "timings":
        _print_json(shell.video_timings())
    elif args.action == "timing":
        _print_json(shell.apply_video_timing(args.standard).raw)
    else:
        shell.set_video_input(args.source)
    return 0


def _progress(done, total):
    """Render a single-line transfer percentage on stderr."""
    if not total:
        return
    print(f"\r  {done * 100 // total:3d}%  {done}/{total} bytes", end="", file=sys.stderr, flush=True)
    if done >= total:
        print(file=sys.stderr)


def _note(message):
    """Emit a progress line on stderr, keeping stdout pure JSON."""
    print(f"  {message}", file=sys.stderr)


def _run_firmware(args):
    """Handle the ``firmware`` subcommand group."""
    releases = firmware.list_releases()
    if args.action == "list":
        shown = releases[: max(1, args.limit)]
        _print_json([{**r.summary(), **({"notes": r.notes} if args.notes else {})} for r in shown])
        return 0
    if args.action == "status":
        latest = firmware.resolve_release("latest", releases=releases)
        with open_shell(serial_number=args.serial_number) as shell:
            current = shell.version().strip()
        behind = firmware.version_key(current) < firmware.version_key(latest.version)
        _print_json({"current": current, "latest": latest.version, "upgradable": behind})
        return 0
    if args.action == "download":
        release = firmware.resolve_release(args.version, releases=releases)
        directory = args.directory or os.path.join(os.getcwd(), "firmware")
        _note(f"{release.tag} -> {directory}")
        path = firmware.download_release(release, directory, progress=_progress)
        image = firmware.inspect_uf2(path)
        _print_json({"release": release.summary(), "path": path, "image": image.summary()})
        return 0
    return _run_upgrade(args, releases)


def _run_upgrade(args, releases):
    """Handle ``firmware upgrade``, which is a dry run unless ``--yes`` is given."""
    release = firmware.resolve_release(args.version, releases=releases, stable_only=args.stable)
    shell = None if args.manual else open_shell(serial_number=args.serial_number)
    try:
        current = shell.version().strip() if shell else None
        if not args.yes:
            _print_json(
                {
                    "dry_run": True,
                    "current": current,
                    "release": release.summary(),
                    "action": "re-run with --yes to download and flash",
                }
            )
            return 0
        if shell is None:
            _note("--manual: expecting the device to already be in bootloader mode")
        result = firmware.upgrade(
            release.version,
            shell=shell,
            serial_number=args.serial_number,
            directory=args.directory,
            stable_only=args.stable,
            force=args.force,
            releases=releases,
            progress=_progress,
            report=_note,
            volume=args.volume,
        )
    finally:
        if shell is not None:
            try:
                shell.close()
            except Exception:
                pass
    _print_json(result)
    return 0


def _run_library(args):
    """Handle the ``library`` subcommand group."""
    releases = firmware.list_releases(product=library.LIBRARY_PRODUCT)
    if args.action == "list":
        _print_json([r.summary() for r in releases[: max(1, args.limit)]])
        return 0
    release = library.resolve_library(
        args.version if args.action != "status" else "latest", releases=releases
    )
    if args.action == "status":
        with open_shell(serial_number=args.serial_number) as shell:
            entries = shell.listdir(f"{library.SHELL_PATH_PREFIX}programs/lzx")
        _print_json(
            {
                "latest": release.version,
                "installed_programs": len([e for e in entries if e.get("type") == "file"]),
                "archive": release.archive.name if release.archive else None,
            }
        )
        return 0
    if args.action == "download":
        directory = args.directory or os.path.join(os.getcwd(), "programs")
        _note(f"{release.tag} -> {directory}")
        path = library.download_library(release, directory, progress=_progress)
        entries = library.library_entries(path)
        _print_json({"release": release.summary(), "path": path, "files": len(entries)})
        return 0
    return _run_install(args, release, releases)


def _run_install(args, release, releases):
    """Handle ``library install``, which is a dry run unless ``--yes`` is given."""
    if not args.yes:
        _print_json(
            {
                "dry_run": True,
                "release": release.summary(),
                "action": "re-run with --yes to download and install",
            }
        )
        return 0
    with open_shell(serial_number=args.serial_number) as shell:
        result = library.install(
            release.version,
            shell=shell,
            serial_number=args.serial_number,
            directory=args.directory,
            force=args.force,
            releases=releases,
            progress=_progress,
            report=_note,
            verify=not args.no_verify,
        )
    _print_json(result)
    return 0


def _run_devices(_args):
    """List attached units and their device nodes."""
    _print_json([vars_of(d) for d in find_devices()])
    return 0


def _run_operators(_args):
    """List the modulation operators and their ids."""
    _print_json(
        {
            info.name: {
                "id": int(info.operator),
                "glyph": info.glyph,
                "category": info.category.value,
                "per_line": info.per_line,
                "transport": info.transport.value,
            }
            for info in OPERATORS.values()
        }
    )
    return 0


#: Subcommand handlers; anything unlisted needs the serial link.
HANDLERS = {
    "devices": _run_devices,
    "operators": _run_operators,
    "firmware": _run_firmware,
    "library": _run_library,
    "set": _run_midi,
    "trigger": _run_midi,
    "preset": _run_midi,
    "clock": _run_midi,
    "cc-map": _run_cc_map,
    "load": _run_load,
    "doctor": _run_device,
    "latency": _run_device,
}


def main(argv=None):
    """Entry point for the ``vmancer`` console script."""
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS.get(args.command, _run_shell)(args)
    except (VmancerError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


def vars_of(info):
    """Render a :class:`~pyvmancer.discovery.DeviceInfo` as a plain dict."""
    return {
        "serial": info.serial_number,
        "rawmidi": info.rawmidi,
        "tty": info.tty,
        "block": info.block,
        "usbfs": info.usbfs_path,
    }


if __name__ == "__main__":
    sys.exit(main())
