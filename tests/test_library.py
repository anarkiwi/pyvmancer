"""Program library releases, archive handling and installing onto the card."""

import hashlib
import io
import json
import os
import zipfile

import pytest

from pyvmancer import library
from pyvmancer.errors import FirmwareError, TransportError
from pyvmancer.firmware import Release
from pyvmancer.library import (
    download_library,
    file_digest,
    install,
    install_library,
    installed_sizes,
    library_entries,
    parse_checksums,
    resolve_library,
)

from .test_firmware import FakeOpener, release_json

# pylint: disable=protected-access

MEMBERS = {
    "programs/lzx/bleach.vmprog": b"bleach payload",
    "programs/lzx/blizzard.vmprog": b"blizzard payload, a little longer",
    "programs/manifest.json": b'{"version": "1.0.3"}',
}


def make_zip(members=None, path=None):
    """Build a library archive, either in memory or at ``path``."""
    members = MEMBERS if members is None else members
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    data = buffer.getvalue()
    if path is None:
        return data
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def library_release(name="videomancer-program-library-1.0.3.zip", size=0, checksums=True):
    """A ``programs/`` release carrying an archive and its digest sidecar."""
    assets = [name] + (["checksums.sha256"] if checksums else [])
    payload = release_json("programs/1.0.3", assets=tuple(assets), prerelease=False, size=size)
    for asset in payload["assets"]:
        if asset["name"] == "checksums.sha256":
            asset["size"] = 104
    return Release.from_json(payload)


class FakeShell:
    """In-memory stand-in for the card's filesystem commands."""

    def __init__(self, files=None, dirs=("sd:/programs", "sd:/programs/lzx")):
        self.files = dict(files or {})
        self.dirs = set(dirs)
        self.puts = []
        self.made = []
        self.fail_on = {}

    def listdir(self, path):
        """Entries directly inside ``path``."""
        if path not in self.dirs:
            raise FirmwareError(f"no such directory: {path}")
        prefix = f"{path}/"
        return [
            {"name": name[len(prefix) :], "type": "file", "size": size}
            for name, size in self.files.items()
            if name.startswith(prefix) and "/" not in name[len(prefix) :]
        ]

    def mkdir(self, path):
        """Create a directory, erroring when it already exists as the firmware does."""
        if path in self.dirs:
            raise FirmwareError(f"exists: {path}")
        self.dirs.add(path)
        self.made.append(path)

    def put_file(self, path, data):
        """Record an upload, optionally failing a scripted number of times."""
        remaining = self.fail_on.get(path, 0)
        if remaining:
            self.fail_on[path] = remaining - 1
            raise TransportError("write to /dev/ttyACM0 failed: (5, 'Input/output error')")
        self.puts.append(path)
        self.files[path] = len(data)

    def stat(self, path):
        """Size of a file on the card."""
        if path not in self.files:
            raise FirmwareError(f"not found: {path}")
        return {"type": "file", "size": self.files[path]}


def test_parse_checksums_reads_sha256sum_format():
    """The sidecar is ordinary ``sha256sum`` output."""
    digest = "a" * 64
    assert parse_checksums(f"{digest}  library.zip\n") == {"library.zip": digest}


def test_parse_checksums_handles_binary_markers_and_paths():
    """``sha256sum -b`` marks binary mode with ``*`` and may carry a path."""
    digest = "b" * 64
    assert parse_checksums(f"{digest} *dist/library.zip") == {"library.zip": digest}


def test_parse_checksums_ignores_noise():
    """Comment and blank lines are not digests."""
    assert not parse_checksums("# a comment\n\nnot-a-digest file.zip\n")


def test_file_digest_matches_hashlib(tmp_path):
    """The streaming digest agrees with hashing the whole file."""
    path = tmp_path / "blob"
    path.write_bytes(b"x" * 5000)
    assert file_digest(str(path)) == hashlib.sha256(b"x" * 5000).hexdigest()


def test_resolve_library_selects_the_programs_product(monkeypatch):
    """Library releases are tagged ``programs/<version>``."""
    seen = {}

    def fake_list(product=None, **_kwargs):
        seen["product"] = product
        return [library_release()]

    monkeypatch.setattr("pyvmancer.firmware.list_releases", fake_list)
    assert resolve_library("latest").version == "1.0.3"
    assert seen["product"] == library.LIBRARY_PRODUCT


def test_download_library_verifies_the_published_digest(tmp_path):
    """The archive is checked against the publisher's sha256, not just its length."""
    data = make_zip()
    digest = hashlib.sha256(data).hexdigest()
    release = library_release(size=len(data))
    opener = FakeOpener(
        {
            "https://example.invalid/programs/1.0.3/checksums.sha256": (
                f"{digest}  videomancer-program-library-1.0.3.zip\n".encode()
            ),
            "https://example.invalid/programs/1.0.3/videomancer": data,
        }
    )
    path = download_library(release, str(tmp_path), opener=opener)
    assert os.path.basename(path) == "videomancer-program-library-1.0.3.zip"


def test_download_library_rejects_a_digest_mismatch(tmp_path):
    """An archive that does not match its published digest is discarded."""
    data = make_zip()
    release = library_release(size=len(data))
    opener = FakeOpener(
        {
            "https://example.invalid/programs/1.0.3/checksums.sha256": (
                f"{'c' * 64}  videomancer-program-library-1.0.3.zip\n".encode()
            ),
            "https://example.invalid/programs/1.0.3/videomancer": data,
        }
    )
    with pytest.raises(FirmwareError, match="sha256"):
        download_library(release, str(tmp_path), opener=opener)
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".zip")]


def test_download_library_rejects_a_non_archive(tmp_path):
    """A payload that is not a zip never reaches the installer."""
    data = b"not a zip at all"
    release = library_release(size=len(data), checksums=False)
    opener = FakeOpener({"https://example.invalid/programs/1.0.3/videomancer": data})
    with pytest.raises(FirmwareError, match="not a zip archive"):
        download_library(release, str(tmp_path), opener=opener)


def test_download_library_needs_an_archive(tmp_path):
    """A release with no zip cannot be installed."""
    release = Release.from_json(release_json("programs/1.0.3", assets=("notes.txt",)))
    with pytest.raises(FirmwareError, match="carries no .zip"):
        download_library(release, str(tmp_path))


def test_library_entries_map_onto_card_paths(tmp_path):
    """Archive members mirror the card layout under ``sd:/``."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    paths = [entry.device_path for entry in library_entries(path)]
    assert paths == [
        "sd:/programs/lzx/bleach.vmprog",
        "sd:/programs/lzx/blizzard.vmprog",
        "sd:/programs/manifest.json",
    ]


def test_library_entries_skip_files_the_card_has_no_use_for(tmp_path):
    """A release that grows a README does not try to install it."""
    members = dict(MEMBERS, **{"programs/README.md": b"# notes"})
    path = make_zip(members, path=str(tmp_path / "lib.zip"))
    assert all(not e.device_path.endswith(".md") for e in library_entries(path))


def test_library_entries_skip_members_outside_the_root(tmp_path):
    """Only the ``programs/`` tree is installed."""
    members = dict(MEMBERS, **{"docs/guide.json": b"{}"})
    path = make_zip(members, path=str(tmp_path / "lib.zip"))
    assert all("guide" not in e.device_path for e in library_entries(path))


def test_library_entries_reject_a_traversing_member(tmp_path):
    """An archive that tries to escape the card root is refused."""
    members = {"programs/../../evil.json": b"{}"}
    path = make_zip(members, path=str(tmp_path / "lib.zip"))
    with pytest.raises(FirmwareError, match="escapes the card root"):
        library_entries(path)


def test_library_entries_carry_sizes(tmp_path):
    """Sizes come from the archive, and are what the skip check compares."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    sizes = {e.device_path: e.size for e in library_entries(path)}
    assert sizes["sd:/programs/lzx/bleach.vmprog"] == len(MEMBERS["programs/lzx/bleach.vmprog"])


def test_installed_sizes_reads_one_listing_per_directory(tmp_path):
    """Sizes are read by listing directories, not by stat-ing every file."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    entries = library_entries(path)
    shell = FakeShell({"sd:/programs/lzx/bleach.vmprog": 14})
    assert installed_sizes(shell, entries)["sd:/programs/lzx/bleach.vmprog"] == 14


def test_installed_sizes_tolerates_a_missing_directory(tmp_path):
    """A card with no library yet lists nothing rather than raising."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())
    assert not installed_sizes(shell, library_entries(path))


def test_install_library_uploads_everything_to_a_bare_card(tmp_path):
    """Nothing on the card means every file is uploaded."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())
    result = install_library(shell, path)
    assert len(result["installed"]) == 3
    assert not result["skipped"]


def test_install_library_skips_files_already_at_size(tmp_path):
    """Re-installing the same release uploads nothing."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    present = {f"sd:/{name}": len(data) for name, data in MEMBERS.items()}
    shell = FakeShell(present)
    result = install_library(shell, path)
    assert not result["installed"]
    assert len(result["skipped"]) == 3


def test_install_library_force_reuploads_everything(tmp_path):
    """``force`` ignores what is already there."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    present = {f"sd:/{name}": len(data) for name, data in MEMBERS.items()}
    shell = FakeShell(present)
    result = install_library(shell, path, force=True)
    assert len(result["installed"]) == 3
    assert not result["skipped"]


def test_install_library_reuploads_a_file_of_the_wrong_size(tmp_path):
    """A truncated file on the card is replaced."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell({"sd:/programs/lzx/bleach.vmprog": 1})
    result = install_library(shell, path)
    assert "sd:/programs/lzx/bleach.vmprog" in result["installed"]


def test_install_library_creates_missing_directories(tmp_path):
    """A card without the library tree gets it created."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())
    install_library(shell, path)
    assert "sd:/programs/lzx" in shell.made


def test_install_library_tolerates_existing_directories(tmp_path):
    """``fs mkdir`` errors on a directory that exists; that is the normal case."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell()
    install_library(shell, path)
    assert not shell.made


def test_install_library_verifies_the_landed_size(tmp_path):
    """A short write is caught by re-reading the size the card reports."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())

    def truncating_put(device_path, data):
        shell.puts.append(device_path)
        shell.files[device_path] = len(data) - 1

    shell.put_file = truncating_put
    with pytest.raises(FirmwareError, match="card holds"):
        install_library(shell, path)


def test_install_library_resumes_after_a_dropped_link(tmp_path):
    """The device drops the link under sustained puts; the upload carries on."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())
    shell.fail_on = {"sd:/programs/lzx/blizzard.vmprog": 1}
    reconnects = []

    def reconnect():
        reconnects.append(True)
        return shell

    result = install_library(shell, path, reconnect=reconnect)
    assert len(result["installed"]) == 3
    assert result["link_drops"] == 1
    assert len(reconnects) == 1


def test_install_library_gives_up_after_repeated_drops(tmp_path):
    """A link that will not stay up is reported rather than retried forever."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())
    shell.fail_on = {"sd:/programs/lzx/bleach.vmprog": 99}
    with pytest.raises(FirmwareError, match="re-run to resume"):
        install_library(shell, path, reconnect=lambda: shell, attempts=3)


def test_install_library_without_reconnect_reports_the_drop(tmp_path):
    """With no way to reconnect, the drop surfaces immediately."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())
    shell.fail_on = {"sd:/programs/lzx/bleach.vmprog": 1}
    with pytest.raises(FirmwareError, match="link lost"):
        install_library(shell, path)


def test_install_library_rejects_an_empty_archive(tmp_path):
    """An archive with nothing installable is an error, not a silent success."""
    path = make_zip({"notes/readme.txt": b"hi"}, path=str(tmp_path / "lib.zip"))
    with pytest.raises(FirmwareError, match="no installable files"):
        install_library(shell=FakeShell(), path=path)


def test_install_needs_a_shell():
    """Installing is a device operation."""
    with pytest.raises(FirmwareError, match="needs an open serial shell"):
        install("latest", shell=None)


def test_install_reports_that_a_restart_is_needed(tmp_path, monkeypatch):
    """The device indexes programs at boot, so new ones appear only after a restart."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    monkeypatch.setattr("pyvmancer.library.download_library", lambda *a, **k: path)
    shell = FakeShell(dirs=())
    result = install("latest", shell=shell, releases=[library_release()])
    assert result["restart_required"] is True


def test_install_says_nothing_about_restarting_when_nothing_changed(tmp_path, monkeypatch):
    """A no-op install needs no restart."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    monkeypatch.setattr("pyvmancer.library.download_library", lambda *a, **k: path)
    present = {f"sd:/{name}": len(data) for name, data in MEMBERS.items()}
    result = install("latest", shell=FakeShell(present), releases=[library_release()])
    assert result["restart_required"] is False


def test_reopener_waits_for_the_device(monkeypatch):
    """After a drop the unit takes a moment to come back."""
    attempts = []

    def flaky(serial_number=None):
        attempts.append(serial_number)
        if len(attempts) < 3:
            raise FirmwareError("not yet")
        return "shell"

    monkeypatch.setattr("pyvmancer.device.open_shell", flaky)
    reopen = library._reopener("SERIAL", settle=0, interval=0, sleep=lambda _s: None)
    assert reopen() == "shell"
    assert attempts == ["SERIAL"] * 3


def test_reopener_gives_up_eventually(monkeypatch):
    """A unit that never returns is reported."""

    def refuse(serial_number=None):
        raise FirmwareError("gone")

    monkeypatch.setattr("pyvmancer.device.open_shell", refuse)
    reopen = library._reopener(settle=0, attempts=2, interval=0, sleep=lambda _s: None)
    with pytest.raises(FirmwareError, match="did not come back"):
        reopen()


def test_download_library_rejects_a_corrupt_archive(tmp_path, monkeypatch):
    """A zip whose members fail their CRC is not installable."""
    data = make_zip()
    release = library_release(size=len(data), checksums=False)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda _self: "programs/lzx/bleach.vmprog")
    opener = FakeOpener({"https://example.invalid/programs/1.0.3/videomancer": data})
    with pytest.raises(FirmwareError, match="archive is corrupt"):
        download_library(release, str(tmp_path), opener=opener)


def test_library_entry_repr_names_the_target(tmp_path):
    """The repr identifies where the file lands."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    assert "sd:/programs" in repr(library_entries(path)[0])


def test_install_library_skips_the_card_root(tmp_path):
    """``sd:/`` itself is never created."""
    path = make_zip({"programs/manifest.json": b"{}"}, path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())
    install_library(shell, path)
    assert shell.made == ["sd:/programs"]


def test_install_library_reports_progress(tmp_path):
    """Progress counts bytes delivered against the archive total."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    seen = []
    install_library(FakeShell(dirs=()), path, progress=lambda done, total: seen.append((done, total)))
    assert seen[-1][0] == seen[-1][1] == sum(len(v) for v in MEMBERS.values())


def test_install_library_rejects_a_member_that_lies_about_its_size(tmp_path):
    """The declared size drives both the skip check and the put handshake."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    entries = library_entries(path)
    monkey = entries[0]
    real_entries = library.library_entries

    def lying(*args, **kwargs):
        found = real_entries(*args, **kwargs)
        found[0].size = monkey.size + 5
        return found

    library.library_entries = lying
    try:
        with pytest.raises(FirmwareError, match="archive declares"):
            install_library(FakeShell(dirs=()), path)
    finally:
        library.library_entries = real_entries


def test_manifest_member_is_installed(tmp_path):
    """The manifest travels with the programs; without it nothing is described."""
    path = make_zip(path=str(tmp_path / "lib.zip"))
    shell = FakeShell(dirs=())
    install_library(shell, path)
    assert "sd:/programs/manifest.json" in shell.puts
    assert json.loads(MEMBERS["programs/manifest.json"])["version"] == "1.0.3"
