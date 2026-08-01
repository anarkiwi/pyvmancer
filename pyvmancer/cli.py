"""Command-line interface: ``vmancer <subcommand>``."""

import argparse
import json
import sys

from . import __version__
from .const import OPERATORS, PARAM_COUNT
from .device import open_midi, open_shell
from .discovery import find_devices
from .errors import VmancerError


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
    sub.add_parser("programs", help="list installed FPGA programs over serial")
    sub.add_parser("presets", help="list factory and user presets over serial")

    load = sub.add_parser("load", help="load an FPGA program by name over serial")
    load.add_argument("name")

    sub.add_parser("manifest", help="show the SD program manifest over serial")
    sub.add_parser("resync", help="bounce the video timing to recover the output")

    hasher = sub.add_parser("hash", help="device-computed sha256 of a file on the SD card")
    hasher.add_argument("path")

    shell_cmd = sub.add_parser("shell", help="run a raw serial shell command")
    shell_cmd.add_argument("words", nargs="+")
    return parser


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
        if args.command == "status":
            _print_json(shell.status())
        elif args.command == "programs":
            _print_json(shell.programs())
        elif args.command == "presets":
            _print_json(shell.presets())
        elif args.command == "load":
            shell.load_program(args.name)
        elif args.command == "manifest":
            manifest = shell.program_manifest()
            _print_json({"version": manifest.version, "programs": manifest.raw.get("programs", [])})
        elif args.command == "resync":
            _print_json({"locked": shell.resync()})
        elif args.command == "hash":
            _print_json(shell.hash_file(args.path))
        elif args.command == "shell":
            print(shell.command(*args.words).payload)
    return 0


def main(argv=None):
    """Entry point for the ``vmancer`` console script."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "devices":
            _print_json([vars_of(d) for d in find_devices()])
            return 0
        if args.command == "operators":
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
        if args.command in ("set", "trigger", "preset", "clock"):
            return _run_midi(args)
        return _run_shell(args)
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
