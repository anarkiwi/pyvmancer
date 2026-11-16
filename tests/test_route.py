"""Vid Route Mode: parsing, naming and reading its settings key."""

import json

import pytest

from pyvmancer.const import ROUTE_SETTING_KEY
from pyvmancer.const import ShellErrorCode
from pyvmancer.errors import ShellError, VmancerError
from pyvmancer.route import describe_route, parse_route, read_route, route_name


@pytest.mark.parametrize(
    "text,want",
    [
        ("1", 1),
        ("hdmi-in", 1),
        ("HDMI In", 1),
        ("hdmi_in", 1),
        ("Auto", 4),
        ("dual in", 2),
        ("0", 0),
        ("9", 9),
    ],
)
def test_parse_route(text, want):
    """The guide's modes parse by name in any spelling; anything else only as a number."""
    assert parse_route(text) == want


def test_parse_route_rejects_unknown_names():
    """A name outside the guide's table is refused, listing the table."""
    with pytest.raises(ValueError, match="analog-in, auto, dual-in, hdmi-in, standalone"):
        parse_route("composite")


def test_route_names():
    """The guide's five modes carry their panel names; any other value is undocumented."""
    assert [route_name(v) for v in range(6)] == [
        "Analog In",
        "HDMI In",
        "Dual In",
        "Standalone",
        "Auto",
        "undocumented mode",
    ]
    assert describe_route(None) == "not reported"


class Settings:
    """``settings get`` answering one scripted payload, or raising."""

    def __init__(self, answer):
        self.answer, self.keys = answer, []

    def get_setting(self, key):
        """The scripted payload for ``key``."""
        self.keys.append(key)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_read_route_reads_key_one():
    """The route is the integer value of settings key 1."""
    shell = Settings(json.dumps({"key": 1, "type": "uint32", "value": 1}))
    assert read_route(shell) == 1 and shell.keys == [ROUTE_SETTING_KEY]


def test_read_route_only_not_found_is_absent_and_bad_payloads_are_errors():
    """Only ``file_not_found`` reads as an absent key; any other error or a non-integer value raises."""
    missing = ShellError(ShellErrorCode.FILE_NOT_FOUND, "setting not found", "settings get 1")
    assert read_route(Settings(missing)) is None
    busy = ShellError(ShellErrorCode.INVALID_STATE, "busy", "settings get 1")
    with pytest.raises(ShellError, match="busy"):
        read_route(Settings(busy))
    for payload in ("garbled", json.dumps({"value": "1"}), json.dumps({"value": True})):
        with pytest.raises(VmancerError, match="settings"):
            read_route(Settings(payload))
