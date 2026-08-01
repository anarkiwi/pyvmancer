"""``video status`` parsing and the advisory lock flags."""

import pytest

from pyvmancer.video import VideoStatus, parse_timings

STATUS = {
    "input": "hdmi",
    "timing": "1080p30",
    "locked": False,
    "overridden": True,
    "hdmi": {"locked": True, "connected": False},
    "analog": {"locked": False},
}


def test_parses_timing_input_and_flags():
    """Nested and top-level keys both contribute to the flat view."""
    status = VideoStatus.from_json(STATUS)
    assert (status.timing, status.input_source) == ("1080p30", "hdmi")
    assert status.overridden
    assert status.raw == STATUS


def test_top_level_lock_tracks_genlock():
    """Overriding the timing clears the top-level flag while the input is fine."""
    status = VideoStatus.from_json(STATUS)
    assert not status.locked
    assert status.source_locked


def test_sub_status_is_authoritative_in_both_directions():
    """A selected input reporting no lock overrides a true top-level flag."""
    data = dict(STATUS, input="analog", locked=True)
    assert not VideoStatus.from_json(data).source_locked


def test_connected_is_advisory():
    """The firmware reports ``connected: false`` while passing video."""
    status = VideoStatus.from_json(STATUS)
    assert status.source_locked
    assert status.raw["hdmi"]["connected"] is False


def test_falls_back_to_the_top_level_flag():
    """Without a sub-status for the selected input, only the genlock flag remains."""
    assert VideoStatus.from_json({"input": "sdi", "locked": True}).source_locked
    assert not VideoStatus.from_json({}).source_locked


@pytest.mark.parametrize("key", ["timing", "output_timing", "out_timing", "format"])
def test_timing_key_drift(key):
    """Firmware naming of the timing field varies; all forms are accepted."""
    assert VideoStatus.from_json({key: "PAL"}).timing == "PAL"


def test_repr_shows_the_flags():
    """The repr carries what a caller has to reason about."""
    assert repr(VideoStatus.from_json(STATUS)) == (
        "VideoStatus(timing='1080p30', input='hdmi', locked=False, overridden=True)"
    )


@pytest.mark.parametrize(
    "usage,expected",
    [
        ("usage: video timing <NTSC|PAL|1080p30>", ["NTSC", "PAL", "1080p30"]),
        ("usage: video timing <NTSC|PAL|...>", ["NTSC", "PAL"]),
        ("video timing < NTSC | PAL >", ["NTSC", "PAL"]),
        ("", []),
        ("no standards here", []),
    ],
)
def test_parse_timings(usage, expected):
    """The accepted standards are discovered from the firmware's own usage string."""
    assert parse_timings(usage) == expected
