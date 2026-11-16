"""Front panel SETTINGS > Vid Route Mode, read from its settings key.

The route decides what the processor is fed, whatever input ``video status`` reports.
Values and names are the serial port guide's Setting Keys table, :data:`~pyvmancer.const.ROUTE_MODES`.
"""

from .const import ROUTE_MODES, ROUTE_SETTING_KEY, ShellErrorCode
from .errors import ShellError
from .shell import read_setting


def route_name(value):
    """The panel name of a route value, else ``undocumented mode``."""
    return ROUTE_MODES.get(value, "undocumented mode")


def describe_route(value):
    """``1 (HDMI In)``, or ``not reported`` for None."""
    return "not reported" if value is None else f"{value} ({route_name(value)})"


def _slug(text):
    """Lowercase, with spaces and underscores as hyphens: ``HDMI In`` is ``hdmi-in``."""
    return "-".join(str(text).lower().replace("_", " ").replace("-", " ").split())


def parse_route(text):
    """A route value from an integer or a mode's name, e.g. ``1``, ``hdmi-in`` or ``Auto``."""
    by_name = {_slug(name): value for value, name in ROUTE_MODES.items()}
    if _slug(text) in by_name:
        return by_name[_slug(text)]
    try:
        return int(text)
    except ValueError:
        raise ValueError(
            f"route {text!r} is neither a settings value nor one of {', '.join(sorted(by_name))}"
        ) from None


def read_route(shell):
    """Vid Route Mode as its settings value, or None when the firmware answers the key as not found.

    Any other error raises rather than reading as absent.
    """
    try:
        return read_setting(shell, ROUTE_SETTING_KEY)
    except ShellError as err:
        if err.code != ShellErrorCode.FILE_NOT_FOUND:
            raise
        return None
