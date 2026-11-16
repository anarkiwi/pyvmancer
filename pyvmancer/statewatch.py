"""Read-only polling of device state, logging every change the device or its front panel makes.

Only reads are sent, one at a time, so a change it records was not made by this host. A boot
count that rises, or the device leaving USB and coming back, is reported as a reboot.
"""

import json
import time
from contextlib import suppress
from datetime import datetime

from tqdm import tqdm

from .const import CC_MAP_SETTING_KEY, PARAM_COUNT, SettingKey, ShellErrorCode, describe_setting, setting_name
from .errors import ShellError, TransportError, VmancerError
from .shell import setting_value

#: Settings keys polled: the ones the device or its panel can change while idle.
WATCHED_KEYS = (
    SettingKey.VIDEO_ROUTE_MODE,
    SettingKey.MIDI_CHANNEL,
    SettingKey.DEVELOPER_MODE,
    SettingKey.CURRENT_PROGRAM,
    SettingKey.PREFERRED_TIMING,
    SettingKey.BOOT_COUNT,
    SettingKey.SELECTED_SLOT,
    SettingKey.TRANSPORT_BPM,
    SettingKey.PROGRAM_AUTHOR,
    *range(CC_MAP_SETTING_KEY, CC_MAP_SETTING_KEY + PARAM_COUNT),
)
#: ``fpga status`` fields polled.
FPGA_FIELDS = ("state", "program", "browse_author")
BOOT_LABEL = f"settings {int(SettingKey.BOOT_COUNT)}"


def _absent(read, *absent):
    """``read()``, or None when the device answers with one of the ``absent`` error codes."""
    try:
        return read()
    except ShellError as err:
        if err.code not in absent:
            raise
        return None


def snapshot(shell, keys=WATCHED_KEYS):
    """The watched state as ``{label: value}``; a key or program state the device lacks is None."""
    state = {
        f"settings {int(key)}": _absent(lambda k=key: setting_value(shell, k), ShellErrorCode.FILE_NOT_FOUND)
        for key in keys
    }
    fpga = shell.command("fpga", "status").shaped(*FPGA_FIELDS)
    state.update({f"fpga {field}": fpga[field] for field in FPGA_FIELDS})
    state["transport bpm_x100"] = shell.command("transport", "status").shaped("bpm_x100")["bpm_x100"]
    state["cc-map"] = json.dumps(
        shell.command("modulation", "cc-map").shaped(assignments=list)["assignments"]
    )
    program = _absent(
        lambda: shell.command("program", "state").shaped("m"), ShellErrorCode.SERVICE_UNAVAILABLE
    )
    state["program state m"] = None if program is None else json.dumps(program["m"])
    return state


def _key(label):
    """The settings key a label names, or None."""
    return int(label.split(" ", 1)[1]) if label.startswith("settings ") else None


def heading(label):
    """A label with its settings key's name from the guide, e.g. ``settings 9 (boot count)``."""
    key = _key(label)
    return label if key is None else f"{label} ({setting_name(key)})"


def shown(label, value):
    """A watched value with the guide's name for it, e.g. ``4 (Auto)``."""
    key = _key(label)
    if value is None:
        return "absent"
    return repr(value) if key is None else describe_setting(key, value)


def describe(label, value):
    """``heading: value``."""
    return f"{heading(label)}: {shown(label, value)}"


def changes(before, after):
    """``(label, old, new)`` for every watched value that differs."""
    return [(key, before.get(key), after.get(key)) for key in after if before.get(key) != after.get(key)]


def rebooted(before, after):
    """True when the boot count rose between two snapshots."""
    old, new = before.get(BOOT_LABEL), after.get(BOOT_LABEL)
    return isinstance(old, int) and isinstance(new, int) and new > old


class StateWatch:
    """Poll :func:`snapshot` every ``interval`` seconds for ``seconds``, emitting a line per change.

    ``reopen`` opens a fresh :class:`~pyvmancer.shell.ShellClient`; it is called again when the
    link is lost, until it succeeds or the time is up.
    """

    def __init__(self, reopen, *, emit=tqdm.write, clock=time.monotonic, sleep=time.sleep, now=datetime.now):
        self.reopen = reopen
        self.emit = emit
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self.reboots = 0
        self.changed = 0

    def _line(self, elapsed, text):
        """Emit ``text`` stamped with wall time and seconds since the start."""
        self.emit(f"{self.now().isoformat(timespec='milliseconds')} +{elapsed:8.1f}s {text}")

    def _reconnect(self, start, seconds, interval):
        """A reopened client, or None once ``seconds`` have passed without one."""
        while self.clock() - start < seconds:
            try:
                return self.reopen()
            except VmancerError:
                self.sleep(interval)
        return None

    def _poll(self, shell, start, seconds, interval):
        """``(shell, snapshot, reconnected)``; a lost link is reopened, and None when it never is."""
        try:
            return shell, snapshot(shell), False
        except TransportError as err:
            self._line(self.clock() - start, f"link lost ({err}); reconnecting")
            with suppress(Exception):
                shell.close()
        shell = self._reconnect(start, seconds, interval)
        return (None, None, True) if shell is None else (shell, snapshot(shell), True)

    def _report(self, elapsed, last, current, lost):
        """Emit a line per change, and one for a reboot."""
        for label, old, new in changes(last, current):
            self.changed += 1
            self._line(elapsed, f"{heading(label)}: {shown(label, old)} -> {shown(label, new)}")
        causes = [
            cause
            for cause, seen in (("boot count rose", rebooted(last, current)), ("USB re-enumeration", lost))
            if seen
        ]
        if causes:
            self.reboots += 1
            self._line(
                elapsed, f"REBOOT ({', '.join(causes)}): {describe(BOOT_LABEL, current.get(BOOT_LABEL))}"
            )

    def run(self, seconds, interval, progress=None):
        """Watch until ``seconds`` have passed; returns the number of changes seen."""
        start = self.clock()
        shell = self.reopen()
        last = snapshot(shell)
        self._line(0.0, "start " + "; ".join(describe(label, value) for label, value in last.items()))
        meter = progress or tqdm(total=round(seconds), unit="s", desc="watching")
        try:
            while self.clock() - start < seconds:
                self.sleep(interval)
                shell, current, lost = self._poll(shell, start, seconds, interval)
                if shell is None:
                    break
                self._report(self.clock() - start, last, current, lost)
                last = current
                meter.update(min(round(self.clock() - start), meter.total) - meter.n)
        finally:
            if shell is not None:
                shell.close()
            meter.update(meter.total - meter.n)
            if progress is None:
                meter.close()
        return self.changed
