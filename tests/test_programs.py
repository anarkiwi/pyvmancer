"""Program library manifest parsing."""

import json

import pytest

from pyvmancer.programs import MANIFEST_PATH, ProgramEntry, ProgramManifest

MANIFEST = {
    "format_version": "1.0",
    "version": "1.0.2",
    "created": "2025-01-01T00:00:00Z",
    "product": "Videomancer",
    "programs": [
        {
            "name": "combing",
            "file": "lzx/combing.vmprog",
            "program_id": "com.lzxindustries.combing",
            "program_name": "Combing",
            "program_version": "1.0.0",
            "categories": ["Signal"],
            "program_type": "processing",
            "description": "Interlace comb artifact simulation",
            "author": "Lars Larsen",
        },
        {"name": "stipple", "file": "lzx/stipple.vmprog"},
    ],
}


@pytest.fixture(name="manifest")
def manifest_fixture():
    """The manifest parsed from its on-card bytes."""
    return ProgramManifest.from_bytes(json.dumps(MANIFEST).encode("utf-8"))


def test_manifest_path_is_on_the_sd_card():
    """The manifest lives on the card, not in firmware."""
    assert MANIFEST_PATH.startswith("sd:/")


def test_manifest_header_fields(manifest):
    """Library metadata is carried alongside the program list."""
    assert (manifest.format_version, manifest.version) == ("1.0", "1.0.2")
    assert (manifest.product, manifest.created) == ("Videomancer", "2025-01-01T00:00:00Z")
    assert manifest.raw == MANIFEST


def test_manifest_entries(manifest):
    """Each row becomes an entry with its shipped metadata."""
    assert len(manifest) == 2
    assert manifest.names == ["combing", "stipple"]
    entry = manifest.get("combing")
    assert (entry.program_name, entry.program_version) == ("Combing", "1.0.0")
    assert (entry.author, entry.program_type) == ("Lars Larsen", "processing")
    assert entry.categories == ("Signal",)
    assert "comb artifact" in entry.description


def test_manifest_lookup_is_case_insensitive(manifest):
    """The device reports program names in its own casing."""
    assert manifest.get("Combing") is manifest.get("combing")
    assert "COMBING" in manifest
    assert manifest.get("nope") is None
    assert "nope" not in manifest


def test_manifest_partial_rows(manifest):
    """Rows carrying only a filename still parse, with the rest absent."""
    entry = manifest.get("stipple")
    assert entry.file == "lzx/stipple.vmprog"
    assert entry.description is None
    assert entry.categories == ()


def test_manifest_covers_only_sd_installed_programs(manifest):
    """Firmware built-ins are absent, so the device list is the longer one."""
    reported = ["combing", "stipple", "builtin_one", "builtin_two"]
    assert manifest.missing(reported) == ["builtin_one", "builtin_two"]


def test_manifest_iteration_and_repr(manifest):
    """Iterating yields entries; the repr counts them."""
    assert [entry.name for entry in manifest] == ["combing", "stipple"]
    assert repr(manifest) == "ProgramManifest(version='1.0.2', programs=2)"
    assert repr(manifest.get("stipple")) == "ProgramEntry('stipple', None, None)"


def test_manifest_without_programs():
    """A manifest naming no programs is empty rather than an error."""
    assert len(ProgramManifest({})) == 0


def test_entry_ignores_unnamed_rows():
    """A row with no name cannot be looked up, so it is dropped."""
    assert len(ProgramManifest({"programs": [{"file": "x.vmprog"}]})) == 0
    assert ProgramEntry({}).name is None
