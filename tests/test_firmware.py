"""Firmware release resolution, UF2 validation and the bootloader flash path."""

import json
import os
import struct
import urllib.error

import pytest

from pyvmancer import firmware
from pyvmancer.errors import FirmwareError
from pyvmancer.firmware import (
    BOOTLOADER_MARKER,
    RP2040_FAMILY_ID,
    UF2_FLAG_FAMILY_ID,
    UF2_FLAG_NOT_MAIN_FLASH,
    UF2_MAGIC_END,
    UF2_MAGIC_START0,
    UF2_MAGIC_START1,
    Release,
    bootloader_info,
    download_release,
    find_bootloader_volumes,
    flash_uf2,
    inspect_uf2,
    list_releases,
    mount_bootloader,
    resolve_release,
    upgrade,
    version_key,
    wait_for_bootloader,
)

# pylint: disable=protected-access


def uf2_block(
    index, total, family=RP2040_FAMILY_ID, flags=UF2_FLAG_FAMILY_ID, payload=256, address=0x10000000
):
    """Render one well-formed 512-byte UF2 block."""
    header = struct.pack(
        "<8I",
        UF2_MAGIC_START0,
        UF2_MAGIC_START1,
        flags,
        address + index * payload,
        payload,
        index,
        total,
        family,
    )
    return header + bytes(min(payload, 476)).ljust(476, b"\x00") + struct.pack("<I", UF2_MAGIC_END)


def make_uf2(blocks=3, **kwargs):
    """Render a whole UF2 image of ``blocks`` blocks."""
    return b"".join(uf2_block(i, blocks, **kwargs) for i in range(blocks))


def write_uf2(tmp_path, name="fw.uf2", data=None, **kwargs):
    """Write a UF2 image to ``tmp_path`` and return its path."""
    path = tmp_path / name
    path.write_bytes(make_uf2(**kwargs) if data is None else data)
    return str(path)


def release_json(tag, assets=("firmware.uf2",), prerelease=True, size=1536):
    """One GitHub release object as the API renders it."""
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "published_at": "2026-08-07T16:04:46Z",
        "body": f"notes for {tag}",
        "assets": [
            {"name": name, "browser_download_url": f"https://example.invalid/{tag}/{name}", "size": size}
            for name in assets
        ],
    }


class FakeResponse:
    """Minimal context-managed HTTP response over a fixed byte string."""

    def __init__(self, payload):
        self.payload = payload
        self.offset = 0

    def read(self, size=None):
        """Return the next ``size`` bytes, or the remainder."""
        end = len(self.payload) if size is None else min(len(self.payload), self.offset + size)
        chunk = self.payload[self.offset : end]
        self.offset = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Opener answering each requested URL from a scripted table."""

    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        for prefix, payload in self.responses.items():
            if url.startswith(prefix):
                return FakeResponse(payload)
        return FakeResponse(b"[]")


def api_pages(*pages):
    """Map paged release-list URLs onto their JSON bodies."""
    base = f"{firmware.GITHUB_API}/repos/{firmware.FIRMWARE_REPO}/releases?per_page=100"
    return {f"{base}&page={n}": json.dumps(page).encode() for n, page in enumerate(pages, start=1)}


def test_version_key_orders_release_candidates_numerically():
    """rc.9 precedes rc.46; a plain string sort would invert them."""
    assert version_key("1.0.0-rc.9") < version_key("1.0.0-rc.46")


def test_version_key_ranks_release_above_its_prereleases():
    """Semver gives a bare version higher precedence than any of its prereleases."""
    assert version_key("1.0.0-rc.46") < version_key("1.0.0")


def test_version_key_ignores_a_leading_v():
    """``v1.2.3`` and ``1.2.3`` are the same version."""
    assert version_key("v1.2.3") == version_key("1.2.3")


def test_version_key_orders_by_major_minor_patch():
    """Core version components compare numerically, not lexically."""
    assert version_key("1.9.0") < version_key("1.10.0")


def test_release_parses_product_and_version_from_the_tag():
    """Tags are ``<product>/<version>``."""
    release = Release.from_json(release_json("videomancer/1.0.0-rc.46"))
    assert (release.product, release.version) == ("videomancer", "1.0.0-rc.46")


def test_release_firmware_picks_the_uf2_asset():
    """Only the ``.uf2`` is the flashable image."""
    release = Release.from_json(release_json("videomancer/1.0.0", assets=("checksums.sha256", "fw.uf2")))
    assert release.firmware.name == "fw.uf2"


def test_release_firmware_is_none_without_an_image():
    """A release carrying no UF2 has no firmware."""
    release = Release.from_json(release_json("videomancer/1.0.0", assets=("notes.txt",)))
    assert release.firmware is None


def test_list_releases_filters_by_product_and_sorts_newest_first():
    """Other products in the same repository are excluded."""
    opener = FakeOpener(
        api_pages(
            [
                release_json("videomancer/1.0.0-rc.9"),
                release_json("tbc2/1.1.0"),
                release_json("videomancer/1.0.0-rc.46"),
            ]
        )
    )
    releases = list_releases(opener=opener)
    assert [r.version for r in releases] == ["1.0.0-rc.46", "1.0.0-rc.9"]


def test_list_releases_follows_pagination():
    """A full page is followed by a request for the next one."""
    opener = FakeOpener(
        api_pages(
            [release_json(f"videomancer/1.0.{n}") for n in range(100)],
            [release_json("videomancer/2.0.0")],
        )
    )
    assert list_releases(opener=opener)[0].version == "2.0.0"
    assert len(opener.urls) == 2


def test_list_releases_rejects_a_non_list_payload():
    """An API error document is not a release list."""
    opener = FakeOpener(api_pages({"message": "Not Found"}))
    with pytest.raises(FirmwareError, match="expected a list"):
        list_releases(opener=opener)


@pytest.fixture(name="releases")
def releases_fixture():
    """Two Videomancer prereleases and one stable release."""
    return [
        Release.from_json(release_json("videomancer/1.0.0-rc.46")),
        Release.from_json(release_json("videomancer/1.0.0-rc.9")),
        Release.from_json(release_json("videomancer/0.9.0", prerelease=False)),
    ]


def test_resolve_release_latest_takes_the_newest(releases):
    """``latest`` is the highest version, prereleases included."""
    assert resolve_release("latest", releases=releases).version == "1.0.0-rc.46"


def test_resolve_release_treats_none_as_latest(releases):
    """``None`` and ``latest`` mean the same thing."""
    assert resolve_release(None, releases=releases).version == "1.0.0-rc.46"


def test_resolve_release_stable_only_skips_prereleases(releases):
    """``stable_only`` drops the rc builds even though they are newer."""
    assert resolve_release("latest", releases=releases, stable_only=True).version == "0.9.0"


def test_resolve_release_stable_only_reports_when_none_exist():
    """Every Videomancer release so far is a prerelease; say so rather than guessing."""
    releases = [Release.from_json(release_json("videomancer/1.0.0-rc.46"))]
    with pytest.raises(FirmwareError, match="every one is a prerelease"):
        resolve_release("latest", releases=releases, stable_only=True)


def test_resolve_release_matches_a_bare_version(releases):
    """A version string selects that release."""
    assert resolve_release("1.0.0-rc.9", releases=releases).tag == "videomancer/1.0.0-rc.9"


def test_resolve_release_matches_a_full_tag(releases):
    """The ``<product>/<version>`` form is accepted too."""
    assert resolve_release("videomancer/1.0.0-rc.9", releases=releases).version == "1.0.0-rc.9"


def test_resolve_release_matches_a_v_prefixed_version(releases):
    """A leading ``v`` is tolerated."""
    assert resolve_release("v1.0.0-rc.9", releases=releases).version == "1.0.0-rc.9"


def test_resolve_release_reports_unknown_versions_with_candidates(releases):
    """An unmatched version names what does exist."""
    with pytest.raises(FirmwareError, match="1.0.0-rc.46"):
        resolve_release("1.0.0-rc.99", releases=releases)


def test_resolve_release_requires_a_flashable_image():
    """Releases without a UF2 cannot be resolved."""
    releases = [Release.from_json(release_json("videomancer/1.0.0", assets=("notes.txt",)))]
    with pytest.raises(FirmwareError, match="no videomancer release carries"):
        resolve_release("latest", releases=releases)


def test_inspect_uf2_summarises_a_valid_image(tmp_path):
    """A well-formed image reports its block count, family and payload size."""
    image = inspect_uf2(write_uf2(tmp_path, blocks=4))
    assert (image.blocks, image.payload_bytes) == (4, 1024)
    assert image.families == {RP2040_FAMILY_ID}
    assert image.start_address == 0x10000000


def test_inspect_uf2_rejects_a_truncated_file(tmp_path):
    """A partial download is not a whole number of blocks."""
    path = write_uf2(tmp_path, data=make_uf2(blocks=2)[:700])
    with pytest.raises(FirmwareError, match="whole number"):
        inspect_uf2(path)


def test_inspect_uf2_rejects_an_empty_file(tmp_path):
    """An empty file is not an image."""
    with pytest.raises(FirmwareError, match="whole number"):
        inspect_uf2(write_uf2(tmp_path, data=b""))


def test_inspect_uf2_rejects_bad_magic(tmp_path):
    """Every block's magic is checked, not just the first."""
    data = bytearray(make_uf2(blocks=3))
    data[512:516] = b"\x00\x00\x00\x00"
    with pytest.raises(FirmwareError, match="block 1 has bad UF2 magic"):
        inspect_uf2(write_uf2(tmp_path, data=bytes(data)))


def test_inspect_uf2_rejects_a_misnumbered_block(tmp_path):
    """Blocks must be numbered in order."""
    data = uf2_block(0, 2) + uf2_block(7, 2)
    with pytest.raises(FirmwareError, match="numbered 7"):
        inspect_uf2(write_uf2(tmp_path, data=data))


def test_inspect_uf2_rejects_a_block_count_mismatch(tmp_path):
    """A file holding fewer blocks than its header declares is incomplete."""
    data = uf2_block(0, 99) + uf2_block(1, 99)
    with pytest.raises(FirmwareError, match="declares 99 blocks"):
        inspect_uf2(write_uf2(tmp_path, data=data))


def test_inspect_uf2_rejects_an_oversized_payload(tmp_path):
    """A block cannot carry more than 476 bytes."""
    data = uf2_block(0, 1, payload=500)
    with pytest.raises(FirmwareError, match="500-byte payload"):
        inspect_uf2(write_uf2(tmp_path, data=data))


def test_inspect_uf2_rejects_the_wrong_mcu_family(tmp_path):
    """A UF2 built for another chip must not reach the bootloader."""
    path = write_uf2(tmp_path, family=0x1C5F21B0)
    with pytest.raises(FirmwareError, match="expected one of"):
        inspect_uf2(path)


def test_inspect_uf2_can_accept_any_family(tmp_path):
    """``families=None`` disables the target check."""
    assert inspect_uf2(write_uf2(tmp_path, family=0x1C5F21B0), families=None).blocks == 3


def test_inspect_uf2_skips_blocks_not_destined_for_flash(tmp_path):
    """``NOT_MAIN_FLASH`` blocks carry no firmware payload."""
    data = uf2_block(0, 2) + uf2_block(1, 2, flags=UF2_FLAG_FAMILY_ID | UF2_FLAG_NOT_MAIN_FLASH)
    assert inspect_uf2(write_uf2(tmp_path, data=data)).payload_bytes == 256


def test_download_release_writes_and_validates(tmp_path):
    """A good download lands under its asset name."""
    image = make_uf2(blocks=3)
    release = Release.from_json(release_json("videomancer/1.0.0", size=len(image)))
    opener = FakeOpener({"https://example.invalid/": image})
    path = download_release(release, str(tmp_path), opener=opener)
    assert os.path.basename(path) == "firmware.uf2"
    with open(path, "rb") as handle:
        assert handle.read() == image


def test_download_release_reports_progress(tmp_path):
    """The progress callback sees monotonically increasing byte counts."""
    image = make_uf2(blocks=3)
    release = Release.from_json(release_json("videomancer/1.0.0", size=len(image)))
    seen = []
    download_release(
        release,
        str(tmp_path),
        opener=FakeOpener({"https://example.invalid/": image}),
        progress=lambda done, total: seen.append((done, total)),
    )
    assert seen[-1] == (len(image), len(image))


def test_download_release_rejects_a_short_transfer(tmp_path):
    """A truncated transfer is an error and leaves nothing behind."""
    release = Release.from_json(release_json("videomancer/1.0.0", size=99999))
    opener = FakeOpener({"https://example.invalid/": make_uf2(blocks=1)})
    with pytest.raises(FirmwareError, match="expected 99999"):
        download_release(release, str(tmp_path), opener=opener)
    assert os.listdir(tmp_path) == []


def test_download_release_rejects_a_corrupt_image(tmp_path):
    """A complete but invalid image is discarded rather than left flashable."""
    payload = b"\x00" * 1024
    release = Release.from_json(release_json("videomancer/1.0.0", size=len(payload)))
    opener = FakeOpener({"https://example.invalid/": payload})
    with pytest.raises(FirmwareError, match="bad UF2 magic"):
        download_release(release, str(tmp_path), opener=opener)
    assert os.listdir(tmp_path) == []


def test_download_release_reuses_a_valid_local_copy(tmp_path):
    """An existing image of the right size is not fetched again."""
    image = make_uf2(blocks=3)
    release = Release.from_json(release_json("videomancer/1.0.0", size=len(image)))
    (tmp_path / "firmware.uf2").write_bytes(image)
    opener = FakeOpener({"https://example.invalid/": b"never read"})
    download_release(release, str(tmp_path), opener=opener)
    assert not opener.urls


def test_download_release_refetches_a_corrupt_local_copy(tmp_path):
    """A local file of the right size that no longer validates is replaced."""
    image = make_uf2(blocks=3)
    release = Release.from_json(release_json("videomancer/1.0.0", size=len(image)))
    (tmp_path / "firmware.uf2").write_bytes(b"\x00" * len(image))
    download_release(release, str(tmp_path), opener=FakeOpener({"https://example.invalid/": image}))
    assert (tmp_path / "firmware.uf2").read_bytes() == image


def test_download_release_needs_an_image():
    """A release with no UF2 cannot be downloaded."""
    release = Release.from_json(release_json("videomancer/1.0.0", assets=("notes.txt",)))
    with pytest.raises(FirmwareError, match="carries no .uf2"):
        download_release(release, "/nonexistent")


def mounts_file(tmp_path, *entries):
    """Write a ``/proc/mounts``-shaped file."""
    path = tmp_path / "mounts"
    path.write_text("".join(f"{d} {p} {t} rw 0 0\n" for d, p, t in entries), encoding="utf-8")
    return str(path)


def test_find_bootloader_volumes_matches_the_uf2_marker(tmp_path):
    """A UF2 bootloader is recognised by INFO_UF2.TXT, whatever its label."""
    volume = tmp_path / "RPI-RP2"
    volume.mkdir()
    (volume / BOOTLOADER_MARKER).write_text("Board-ID: RPI-RP2\n", encoding="utf-8")
    mounts = mounts_file(tmp_path, ("/dev/sdd", str(volume), "vfat"))
    assert find_bootloader_volumes(mounts) == [str(volume)]


def test_find_bootloader_volumes_ignores_plain_fat_media(tmp_path):
    """An ordinary SD card mount is not a bootloader."""
    volume = tmp_path / "SDCARD"
    volume.mkdir()
    mounts = mounts_file(tmp_path, ("/dev/sdc", str(volume), "vfat"))
    assert not find_bootloader_volumes(mounts)


def test_find_bootloader_volumes_ignores_other_filesystems(tmp_path):
    """Only FAT-family mounts are considered."""
    volume = tmp_path / "root"
    volume.mkdir()
    (volume / BOOTLOADER_MARKER).write_text("x", encoding="utf-8")
    mounts = mounts_file(tmp_path, ("/dev/sda1", str(volume), "ext4"))
    assert not find_bootloader_volumes(mounts)


def test_find_bootloader_volumes_decodes_escaped_mount_points(tmp_path):
    """``/proc/mounts`` octal-escapes spaces in mount points."""
    volume = tmp_path / "RPI RP2"
    volume.mkdir()
    (volume / BOOTLOADER_MARKER).write_text("x", encoding="utf-8")
    escaped = str(volume).replace(" ", r"\040")
    mounts = mounts_file(tmp_path, ("/dev/sdd", escaped, "vfat"))
    assert find_bootloader_volumes(mounts) == [str(volume)]


def test_find_bootloader_volumes_survives_a_missing_mounts_file():
    """Platforms without /proc/mounts report nothing rather than raising."""
    assert not find_bootloader_volumes("/nonexistent/mounts")


def test_bootloader_info_parses_the_marker(tmp_path):
    """INFO_UF2.TXT is colon-separated key/value lines."""
    (tmp_path / BOOTLOADER_MARKER).write_text(
        "UF2 Bootloader v3.0\nModel: Raspberry Pi RP2\nBoard-ID: RPI-RP2\n", encoding="utf-8"
    )
    assert bootloader_info(str(tmp_path))["Board-ID"] == "RPI-RP2"


def test_bootloader_info_is_empty_when_absent(tmp_path):
    """A volume without the marker yields no info."""
    assert not bootloader_info(str(tmp_path))


class FakeRunner:
    """Stand-in for ``subprocess.run`` returning scripted output."""

    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        return self


def test_mount_bootloader_parses_the_mount_point():
    """udisksctl reports where it mounted the volume."""
    runner = FakeRunner(stdout="Mounted /dev/sdd at /media/user/RPI-RP2.\n")
    assert mount_bootloader(block="/dev/sdd", runner=runner) == "/media/user/RPI-RP2"


def test_mount_bootloader_handles_an_already_mounted_volume():
    """An automounted volume is reported by udisksctl as already mounted."""
    runner = FakeRunner(stderr="Error mounting: already mounted at: /media/user/RPI-RP2\n")
    assert mount_bootloader(block="/dev/sdd", runner=runner) == "/media/user/RPI-RP2"


def test_mount_bootloader_reports_a_failure():
    """A refusal surfaces udisksctl's own message."""
    runner = FakeRunner(stderr="Error mounting /dev/sdd: not authorized\n")
    with pytest.raises(FirmwareError, match="not authorized"):
        mount_bootloader(block="/dev/sdd", runner=runner)


def test_mount_bootloader_needs_a_device():
    """Nothing to mount when no bootloader is attached."""
    with pytest.raises(FirmwareError, match="no UF2 bootloader block device"):
        mount_bootloader(block=None, runner=FakeRunner())


def test_wait_for_bootloader_times_out(monkeypatch):
    """A device that never enters the bootloader is reported, not waited on forever."""
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_volumes", lambda *a: [])
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_block", lambda *a, **k: None)
    with pytest.raises(FirmwareError, match="within 0"):
        wait_for_bootloader(timeout=0.0, sleep=lambda _s: None)


def test_wait_for_bootloader_returns_a_mounted_volume(monkeypatch):
    """An already-mounted bootloader is used as-is."""
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_volumes", lambda *a: ["/media/RPI-RP2"])
    assert wait_for_bootloader(timeout=0.0) == "/media/RPI-RP2"


def test_flash_uf2_copies_the_image(tmp_path):
    """The image is copied onto the volume under its own name."""
    source = write_uf2(tmp_path, name="fw.uf2", blocks=3)
    volume = tmp_path / "volume"
    volume.mkdir()
    sent = flash_uf2(source, str(volume))
    assert sent == os.path.getsize(source)
    with open(source, "rb") as handle:
        assert (volume / "fw.uf2").read_bytes() == handle.read()


def test_flash_uf2_tolerates_the_bootloader_rebooting_on_the_last_block(tmp_path, monkeypatch):
    """The volume vanishing once every byte is delivered is the expected ending."""
    source = write_uf2(tmp_path, name="fw.uf2", blocks=2)
    volume = tmp_path / "volume"
    volume.mkdir()

    def exploding_fsync(_fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", exploding_fsync)
    assert flash_uf2(source, str(volume)) == os.path.getsize(source)


def test_flash_uf2_reports_a_failure_before_the_image_is_delivered(tmp_path):
    """An error with bytes still outstanding is a real failure."""
    source = write_uf2(tmp_path, name="fw.uf2", blocks=2)
    with pytest.raises(FirmwareError, match="failed after 0/1024 bytes"):
        flash_uf2(source, str(tmp_path / "no-such-volume"))


class FakeShell:
    """Shell stub reporting a fixed firmware version."""

    def __init__(self, version="1.0.0-rc.46"):
        self._version = version
        self.rebooted = False
        self.closed = False

    def version(self):
        """The running firmware version."""
        return self._version

    def serial_number(self):
        """A fixed hardware id."""
        return "E464B0605F113625"

    def reboot_bootloader(self):
        """Record that the bootloader was entered."""
        self.rebooted = True

    def close(self):
        """Record the close."""
        self.closed = True


def test_upgrade_skips_a_device_already_on_the_release(releases):
    """Re-flashing the running version is pointless, so it is refused by default."""
    shell = FakeShell("1.0.0-rc.46")
    result = upgrade("latest", shell=shell, releases=releases)
    assert result == {
        "flashed": False,
        "reason": "already current",
        "version": "1.0.0-rc.46",
        "release": releases[0].summary(),
    }
    assert not shell.rebooted


def test_upgrade_reflashes_the_current_version_when_forced(releases, monkeypatch, tmp_path):
    """``force`` re-flashes anyway, for recovering a bad install."""
    image = make_uf2(blocks=2)
    monkeypatch.setattr(
        "pyvmancer.firmware.download_release",
        lambda *a, **k: write_uf2(tmp_path, name="fw.uf2", data=image),
    )
    monkeypatch.setattr("pyvmancer.firmware.wait_for_bootloader", lambda **k: str(tmp_path))
    monkeypatch.setattr("pyvmancer.firmware.flash_uf2", lambda *a, **k: len(image))
    monkeypatch.setattr("pyvmancer.firmware.wait_for_device", lambda **k: _FakeInfo())
    monkeypatch.setattr("pyvmancer.firmware.confirm_version", lambda *a, **k: "1.0.0-rc.46")
    shell = FakeShell("1.0.0-rc.46")
    result = upgrade("latest", shell=shell, releases=releases, force=True, sleep=lambda _s: None)
    assert result["flashed"] is True
    assert shell.rebooted and shell.closed


class _FakeInfo:
    """DeviceInfo stub for a re-enumerated device."""

    serial_number = "E464B0605F113625"
    tty = "/dev/ttyACM0"
    block = "/dev/sdd"


def test_headers_carry_a_token_from_the_environment(monkeypatch):
    """A GITHUB_TOKEN lifts the unauthenticated API rate limit."""
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    assert firmware._headers()["Authorization"] == "Bearer secret"


def test_headers_omit_authorization_without_a_token(monkeypatch):
    """Anonymous requests still work, just with a lower rate limit."""
    for name in firmware.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    assert "Authorization" not in firmware._headers()


def test_urlopen_explains_a_rate_limit(monkeypatch):
    """A 403 is nearly always rate limiting, so the message says what to do."""
    for name in firmware.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)

    def refuse(_request, timeout=None):
        raise urllib.error.HTTPError("https://api.invalid", 403, "Forbidden", {}, None)

    with pytest.raises(FirmwareError, match="set GITHUB_TOKEN"):
        firmware._urlopen("https://api.invalid", opener=refuse)


def test_urlopen_reports_other_http_errors(monkeypatch):
    """A 404 is reported without the rate-limit advice."""
    for name in firmware.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)

    def refuse(_request, timeout=None):
        raise urllib.error.HTTPError("https://api.invalid", 404, "Not Found", {}, None)

    with pytest.raises(FirmwareError, match="HTTP 404") as err:
        firmware._urlopen("https://api.invalid", opener=refuse)
    assert "GITHUB_TOKEN" not in str(err.value)


def test_urlopen_reports_a_network_failure(monkeypatch):
    """An unreachable host is a firmware error, not a bare OSError."""
    for name in firmware.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)

    def refuse(_request, timeout=None):
        raise OSError("name resolution failed")

    with pytest.raises(FirmwareError, match="name resolution failed"):
        firmware._urlopen("https://api.invalid", opener=refuse)


def test_resolve_release_fetches_when_given_no_list(monkeypatch, releases):
    """Omitting ``releases`` fetches them."""
    monkeypatch.setattr("pyvmancer.firmware.list_releases", lambda **k: releases)
    assert resolve_release("latest").version == "1.0.0-rc.46"


def test_repr_is_informative(tmp_path, releases):
    """Reprs name the object well enough to debug from a log."""
    assert "1.0.0-rc.46" in repr(releases[0])
    assert "firmware.uf2" in repr(releases[0].firmware)
    assert "blocks=3" in repr(inspect_uf2(write_uf2(tmp_path)))


def test_mount_bootloader_reports_a_missing_udisksctl():
    """udisksctl is not installed everywhere."""

    def missing(_command, **kwargs):
        raise OSError("No such file or directory")

    with pytest.raises(FirmwareError, match="cannot run udisksctl"):
        mount_bootloader(block="/dev/sdd", runner=missing)


def test_mount_bootloader_falls_back_to_a_mounted_volume(monkeypatch):
    """Unparseable output still succeeds if the volume did come up."""
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_volumes", lambda *a: ["/media/RPI-RP2"])
    runner = FakeRunner(stdout="something unexpected")
    assert mount_bootloader(block="/dev/sdd", runner=runner) == "/media/RPI-RP2"


def test_volume_nodes_prefers_the_partition(tmp_path):
    """The RP2040 bootloader's FAT volume is a partition; the disk node is not mountable."""
    (tmp_path / "sdc" / "sdc1").mkdir(parents=True)
    assert firmware.volume_nodes("/dev/sdc", sysfs=str(tmp_path)) == ["/dev/sdc1"]


def test_volume_nodes_falls_back_to_the_whole_disk(tmp_path):
    """An unpartitioned device is mounted directly."""
    (tmp_path / "sdc").mkdir()
    assert firmware.volume_nodes("/dev/sdc", sysfs=str(tmp_path)) == ["/dev/sdc"]


def test_mount_bootloader_explains_a_polkit_refusal():
    """A headless session cannot authorize udisks, so point at the manual mount."""
    runner = FakeRunner(stderr="GDBus.Error:...NotAuthorizedCanObtain: Not authorized\n")
    with pytest.raises(FirmwareError, match="Mount it by hand"):
        mount_bootloader(block="/dev/sdc1", runner=runner)


def test_find_bootloader_block_uses_the_bootloader_usb_ids(monkeypatch):
    """The RP2040 bootloader enumerates under its own VID/PID, not the Videomancer's."""
    seen = {}

    def fake_find(vid=None, pid=None):
        seen.update(vid=vid, pid=pid)
        return [_FakeInfo()]

    monkeypatch.setattr("pyvmancer.firmware.find_devices", fake_find)
    assert firmware.find_bootloader_block() == "/dev/sdd"
    assert seen == {"vid": firmware.BOOTLOADER_VID, "pid": firmware.BOOTLOADER_PID}


def test_wait_for_bootloader_mounts_an_unmounted_bootloader(monkeypatch):
    """A bootloader nothing automounted is mounted on the way past."""
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_volumes", lambda *a: [])
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_block", lambda *a, **k: "/dev/sdd")
    monkeypatch.setattr("pyvmancer.firmware.mount_bootloader", lambda: "/media/RPI-RP2")
    assert wait_for_bootloader(timeout=1.0, sleep=lambda _s: None) == "/media/RPI-RP2"


def test_upgrade_uses_a_supplied_volume(releases, monkeypatch, tmp_path):
    """``volume`` skips discovery, for a bootloader mounted by hand."""
    image = make_uf2(blocks=2)
    monkeypatch.setattr(
        "pyvmancer.firmware.download_release",
        lambda *a, **k: write_uf2(tmp_path, name="fw.uf2", data=image),
    )

    def unreachable(**_kwargs):
        raise AssertionError("discovery must not run when a volume is given")

    monkeypatch.setattr("pyvmancer.firmware.wait_for_bootloader", unreachable)
    monkeypatch.setattr("pyvmancer.firmware.flash_uf2", lambda *a, **k: len(image))
    monkeypatch.setattr("pyvmancer.firmware.wait_for_device", lambda **k: _FakeInfo())
    monkeypatch.setattr("pyvmancer.firmware.confirm_version", lambda *a, **k: "1.0.0-rc.46")
    result = upgrade("latest", releases=releases, volume="/mnt", sleep=lambda _s: None)
    assert result["mountpoint"] == "/mnt"


def test_wait_for_bootloader_reports_the_last_mount_failure(monkeypatch):
    """When mounting keeps failing, the timeout says why."""
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_volumes", lambda *a: [])
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_block", lambda *a, **k: "/dev/sdd")

    def refuse():
        raise FirmwareError("not authorized")

    monkeypatch.setattr("pyvmancer.firmware.mount_bootloader", refuse)
    with pytest.raises(FirmwareError, match="not authorized"):
        wait_for_bootloader(timeout=0.0, sleep=lambda _s: None)


def test_wait_for_bootloader_polls_until_it_appears(monkeypatch):
    """The volume takes a moment to show up after the reboot."""
    volumes = [[], [], ["/media/RPI-RP2"]]
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_volumes", lambda *a: volumes.pop(0))
    monkeypatch.setattr("pyvmancer.firmware.find_bootloader_block", lambda *a, **k: None)
    assert wait_for_bootloader(timeout=5.0, sleep=lambda _s: None) == "/media/RPI-RP2"


def test_flash_uf2_reports_progress(tmp_path):
    """The flash reports bytes delivered, like the download does."""
    source = write_uf2(tmp_path, name="fw.uf2", blocks=4)
    volume = tmp_path / "volume"
    volume.mkdir()
    seen = []
    flash_uf2(source, str(volume), chunk=512, progress=lambda done, total: seen.append(done))
    assert seen == [512, 1024, 1536, 2048]


def test_wait_for_device_returns_the_matching_unit(monkeypatch):
    """A serial number selects one unit out of several."""
    monkeypatch.setattr("pyvmancer.firmware.find_devices", lambda: [_FakeInfo()])
    assert firmware.wait_for_device(serial_number="E464B0605F113625").tty == "/dev/ttyACM0"


def test_wait_for_device_ignores_other_units(monkeypatch):
    """A different device coming back is not the one that was flashed."""
    monkeypatch.setattr("pyvmancer.firmware.find_devices", lambda: [_FakeInfo()])
    with pytest.raises(FirmwareError, match="did not re-enumerate"):
        firmware.wait_for_device(serial_number="OTHER", timeout=0.0, sleep=lambda _s: None)


def test_wait_for_device_times_out(monkeypatch):
    """A unit that never comes back is reported."""
    monkeypatch.setattr("pyvmancer.firmware.find_devices", lambda: [])
    with pytest.raises(FirmwareError, match="did not re-enumerate"):
        firmware.wait_for_device(timeout=0.0, sleep=lambda _s: None)


def test_confirm_version_retries_while_the_link_comes_up(monkeypatch):
    """The CDC interface lags the USB reset, so the first reads can fail."""
    attempts = []

    def flaky(serial_number=None):
        attempts.append(serial_number)
        if len(attempts) < 3:
            raise OSError("not ready")
        return _ShellContext("1.0.0-rc.46")

    monkeypatch.setattr("pyvmancer.device.open_shell", flaky)
    assert firmware.confirm_version(sleep=lambda _s: None) == "1.0.0-rc.46"
    assert len(attempts) == 3


def test_confirm_version_gives_up_and_returns_none(monkeypatch):
    """A link that never answers leaves the version unknown rather than raising."""

    def refuse(serial_number=None):
        raise OSError("not ready")

    monkeypatch.setattr("pyvmancer.device.open_shell", refuse)
    assert firmware.confirm_version(attempts=2, sleep=lambda _s: None) is None


class _ShellContext:
    """Context-managed shell stub for confirm_version."""

    def __init__(self, version):
        self._version = version

    def version(self):
        """The running firmware version."""
        return self._version

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
