"""Vid Route Mode: parsing, naming and reading its settings key."""

import json

import pytest

from pyvmancer.const import ROUTE_SETTING_KEY
from pyvmancer.errors import ShellTimeoutError, VmancerError
from pyvmancer.route import describe_route, parse_route, read_route, route_name


@pytest.mark.parametrize(
    "text,want", [("1", 1), ("hdmi-in", 1), ("HDMI In", 1), ("hdmi_in", 1), ("Auto", 4), ("3", 3)]
)
def test_parse_route(text, want):
    """Observed modes parse by name in any spelling; anything else only as a number."""
    assert parse_route(text) == want


def test_parse_route_rejects_unobserved_names():
    """A mode whose value was never observed cannot be named."""
    with pytest.raises(ValueError, match="auto, hdmi-in"):
        parse_route("dual in")


def test_route_names():
    """Only observed values carry a panel name."""
    assert (route_name(1), route_name(4), route_name(2)) == ("HDMI In", "Auto", "unconfirmed mode")
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


def test_read_route_timeout_raises_and_bad_payloads_are_errors():
    """A silent shell is not an absent key, and a reply with no integer value is an error."""
    with pytest.raises(ShellTimeoutError):
        read_route(Settings(ShellTimeoutError("settings get 1", 5)))
    for payload in ("garbled", json.dumps({"value": "1"}), json.dumps({"value": True})):
        with pytest.raises(VmancerError, match="settings key 1"):
            read_route(Settings(payload))
