"""Firmware releases and the UF2 bootloader upgrade path.

LZX publishes Videomancer firmware as GitHub releases on
``lzxindustries/videomancer-firmware``. That repository carries several products,
tagged ``<product>/<version>``, so a Videomancer release is selected by tag
prefix rather than by GitHub's notion of "latest" -- see :func:`resolve_release`.

Each Videomancer release ships one ``.uf2``. The device flashes it through the
RP2040 bootloader: ``reboot bootloader`` drops the USB link and the unit
re-enumerates as a mass-storage volume that the UF2 is copied onto, after which
it reboots itself into the new firmware.
"""

import glob
import json
import os
import re
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .const import VERSION
from .discovery import find_devices
from .errors import FirmwareError

#: Repository publishing LZX firmware binaries for every product.
FIRMWARE_REPO = "lzxindustries/videomancer-firmware"
#: Tag prefix selecting Videomancer releases out of that repository.
FIRMWARE_PRODUCT = "videomancer"
GITHUB_API = "https://api.github.com"
#: Environment variables consulted for a GitHub token, in order.
TOKEN_ENV = ("GITHUB_TOKEN", "GH_TOKEN")
HTTP_TIMEOUT = 30.0
DOWNLOAD_CHUNK = 1 << 16

UF2_BLOCK_SIZE = 512
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_NOT_MAIN_FLASH = 0x0001
UF2_FLAG_FAMILY_ID = 0x2000
#: Largest payload a UF2 block can carry.
UF2_PAYLOAD_MAX = 476
#: UF2 family id of the RP2040, the MCU the Videomancer's UF2s target.
RP2040_FAMILY_ID = 0xE48BFF56

#: USB ids the RP2040 BOOTSEL bootloader enumerates with.
BOOTLOADER_VID = 0x2E8A
BOOTLOADER_PID = 0x0003
#: File every UF2 bootloader volume carries; how one is recognised regardless of label.
BOOTLOADER_MARKER = "INFO_UF2.TXT"
SYS_BLOCK = "/sys/block"
#: Seconds to wait for the bootloader volume after ``reboot bootloader``.
BOOTLOADER_TIMEOUT = 60.0
#: Seconds to wait for the device to re-enumerate after flashing.
REBOOT_TIMEOUT = 90.0
POLL_INTERVAL = 1.0


def _token(token=None):
    """Explicit token, else the first of :data:`TOKEN_ENV` that is set."""
    if token:
        return token
    return next((os.environ[name] for name in TOKEN_ENV if os.environ.get(name)), None)


def _headers(token=None):
    """Request headers for the GitHub REST API."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"pyvmancer/{VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resolved = _token(token)
    if resolved:
        headers["Authorization"] = f"Bearer {resolved}"
    return headers


def _urlopen(url, token=None, opener=None):
    """Open ``url`` with GitHub headers applied, raising :class:`FirmwareError` on failure."""
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        return (opener or urllib.request.urlopen)(request, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as err:
        detail = f"HTTP {err.code} {err.reason}"
        if err.code in (401, 403, 429):
            detail += "; set GITHUB_TOKEN if this is rate limiting"
        raise FirmwareError(f"{url}: {detail}") from err
    except OSError as err:
        raise FirmwareError(f"{url}: {err}") from err


def _pre_key(pre):
    """Semver precedence key for the dot-separated identifiers of a prerelease."""
    parts = []
    for part in re.split(r"[.\-]", pre):
        if not part:
            continue
        parts.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return tuple(parts)


def version_key(version):
    """Sort key ordering versions by semver precedence.

    Numeric identifiers compare numerically, so ``1.0.0-rc.9`` sorts below
    ``1.0.0-rc.46`` where a plain string sort would invert them, and a release
    outranks any prerelease of the same core version.
    """
    core, _, pre = str(version).strip().lstrip("vV").partition("-")
    numbers = tuple(int(part) if part.isdigit() else 0 for part in core.split("."))
    return (numbers, 0, _pre_key(pre)) if pre else (numbers, 1, ())


class Asset:
    """One downloadable file attached to a release."""

    __slots__ = ("name", "url", "size")

    def __init__(self, name, url, size=0):
        self.name = name
        self.url = url
        self.size = int(size or 0)

    def __repr__(self):
        return f"Asset(name={self.name!r}, size={self.size})"


class Release:
    """One published firmware release, tagged ``<product>/<version>``."""

    __slots__ = ("tag", "product", "version", "prerelease", "published_at", "assets", "notes")

    def __init__(self, tag, prerelease=False, published_at=None, assets=(), notes=""):
        self.tag = tag
        self.product, _, self.version = tag.rpartition("/")
        self.prerelease = bool(prerelease)
        self.published_at = published_at
        self.assets = list(assets)
        self.notes = notes or ""

    @classmethod
    def from_json(cls, payload):
        """Build a release from one GitHub API release object."""
        assets = [
            Asset(a.get("name", ""), a.get("browser_download_url", ""), a.get("size", 0))
            for a in payload.get("assets", [])
        ]
        return cls(
            tag=payload.get("tag_name", ""),
            prerelease=payload.get("prerelease", False),
            published_at=payload.get("published_at"),
            assets=assets,
            notes=payload.get("body", ""),
        )

    def asset_for(self, *suffixes):
        """First attached asset whose name ends in one of ``suffixes``."""
        wanted = tuple(s.lower() for s in suffixes)
        return next((a for a in self.assets if a.name.lower().endswith(wanted)), None)

    @property
    def firmware(self):
        """The ``.uf2`` asset, or ``None`` when the release carries no firmware image."""
        return self.asset_for(".uf2")

    @property
    def archive(self):
        """The ``.zip`` asset, which is how the program library ships."""
        return self.asset_for(".zip")

    @property
    def checksums(self):
        """The digest sidecar, named ``checksums.sha256`` or ``<asset>.sha256``."""
        return self.asset_for(".sha256")

    @property
    def payload(self):
        """The asset this release exists to deliver, image or archive."""
        return self.firmware or self.archive

    @property
    def sort_key(self):
        """Semver precedence key for this release's version."""
        return version_key(self.version)

    def summary(self):
        """Plain dict of the fields worth reporting."""
        payload = self.payload
        return {
            "tag": self.tag,
            "product": self.product,
            "version": self.version,
            "prerelease": self.prerelease,
            "published": self.published_at,
            "asset": payload.name if payload else None,
            "size": payload.size if payload else None,
        }

    def __repr__(self):
        return f"Release(tag={self.tag!r}, prerelease={self.prerelease})"


def list_releases(product=FIRMWARE_PRODUCT, repo=FIRMWARE_REPO, token=None, opener=None, pages=10):
    """Every release for ``product``, newest first by version precedence.

    ``product`` filters on the tag prefix; pass ``None`` to keep every product in
    the repository.
    """
    releases = []
    for page in range(1, pages + 1):
        url = f"{GITHUB_API}/repos/{repo}/releases?per_page=100&page={page}"
        with _urlopen(url, token=token, opener=opener) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise FirmwareError(f"{url}: expected a list of releases")
        releases.extend(Release.from_json(item) for item in payload)
        if len(payload) < 100:
            break
    if product:
        prefix = f"{product}/"
        releases = [r for r in releases if r.tag.startswith(prefix)]
    return sorted(releases, key=lambda r: r.sort_key, reverse=True)


def resolve_release(spec="latest", releases=None, product=FIRMWARE_PRODUCT, stable_only=False, **kwargs):
    """Find the release named by ``spec``.

    ``spec`` is ``latest`` (or ``None``) for the newest release, or a version
    such as ``1.0.0-rc.46``; a full ``videomancer/1.0.0-rc.46`` tag and a
    leading ``v`` are both accepted. Every Videomancer release to date is
    flagged as a prerelease on GitHub, so ``latest`` includes prereleases unless
    ``stable_only`` is set -- which is also why GitHub's own "latest release"
    endpoint is not used here.
    """
    if releases is None:
        releases = list_releases(product=product, **kwargs)
    usable = [r for r in releases if r.payload]
    if not usable:
        raise FirmwareError(f"no {product or 'firmware'} release carries a downloadable asset")
    if spec is None or str(spec).strip().lower() == "latest":
        candidates = [r for r in usable if not r.prerelease] if stable_only else usable
        if not candidates:
            raise FirmwareError(
                f"no stable {product or 'firmware'} release exists; every one is a prerelease"
            )
        return candidates[0]
    wanted = str(spec).strip()
    bare = wanted.rpartition("/")[2].lstrip("vV")
    for release in usable:
        if wanted in (release.tag, release.version) or bare == release.version.lstrip("vV"):
            return release
    known = ", ".join(r.version for r in usable[:5])
    raise FirmwareError(f"no {product or 'firmware'} release matches {spec!r}; recent versions: {known}")


class Uf2Image:
    """Validated summary of a UF2 file."""

    __slots__ = ("path", "blocks", "families", "payload_bytes", "start_address", "end_address")

    def __init__(self, path, blocks, families, payload_bytes, start_address, end_address):
        self.path = path
        self.blocks = blocks
        self.families = families
        self.payload_bytes = payload_bytes
        self.start_address = start_address
        self.end_address = end_address

    def summary(self):
        """Plain dict of the fields worth reporting."""
        return {
            "blocks": self.blocks,
            "families": [f"{family:#010x}" for family in sorted(self.families)],
            "payload_bytes": self.payload_bytes,
            "start_address": f"{self.start_address:#010x}" if self.start_address is not None else None,
        }

    def __repr__(self):
        return f"Uf2Image(blocks={self.blocks}, payload_bytes={self.payload_bytes})"


def _parse_block(path, index, block, expected):
    """Validate one UF2 block, returning ``(flags, address, payload, family)``."""
    start0, start1, flags, address, payload, block_no, total, family = struct.unpack("<8I", block[:32])
    end = struct.unpack("<I", block[508:512])[0]
    if (start0, start1, end) != (UF2_MAGIC_START0, UF2_MAGIC_START1, UF2_MAGIC_END):
        raise FirmwareError(f"{path}: block {index} has bad UF2 magic")
    if block_no != index:
        raise FirmwareError(f"{path}: block {index} is numbered {block_no}")
    if total != expected:
        raise FirmwareError(f"{path}: block {index} declares {total} blocks, file holds {expected}")
    if payload > UF2_PAYLOAD_MAX:
        raise FirmwareError(f"{path}: block {index} declares a {payload}-byte payload")
    return flags, address, payload, family


def inspect_uf2(path, families=(RP2040_FAMILY_ID,)):
    """Parse and validate a UF2 file, returning a :class:`Uf2Image`.

    Every block is checked, because a UF2 that is truncated or targets the wrong
    MCU must be rejected here rather than half-written to a bootloader. Pass
    ``families=None`` to accept any target family.
    """
    size = os.path.getsize(path)
    if size == 0 or size % UF2_BLOCK_SIZE:
        raise FirmwareError(f"{path}: not a whole number of {UF2_BLOCK_SIZE}-byte UF2 blocks ({size} bytes)")
    expected = size // UF2_BLOCK_SIZE
    seen = set()
    payload_bytes = 0
    addresses = []
    with open(path, "rb") as handle:
        for index in range(expected):
            flags, address, payload, family = _parse_block(path, index, handle.read(UF2_BLOCK_SIZE), expected)
            if flags & UF2_FLAG_NOT_MAIN_FLASH:
                continue
            payload_bytes += payload
            addresses.append((address, address + payload))
            if flags & UF2_FLAG_FAMILY_ID:
                seen.add(family)
    if families is not None and not seen & set(families):
        wanted = ", ".join(f"{f:#010x}" for f in families)
        got = ", ".join(f"{f:#010x}" for f in sorted(seen)) or "none"
        raise FirmwareError(f"{path}: targets family {got}, expected one of {wanted}")
    return Uf2Image(
        path=path,
        blocks=expected,
        families=seen,
        payload_bytes=payload_bytes,
        start_address=min(a for a, _ in addresses) if addresses else None,
        end_address=max(b for _, b in addresses) if addresses else None,
    )


def fetch_bytes(asset, token=None, opener=None):
    """Download a small asset straight into memory, for digests and manifests."""
    with _urlopen(asset.url, token=token, opener=opener) as response:
        return response.read()


def download_asset(asset, directory, token=None, opener=None, progress=None, reuse=True, validate=None):
    """Fetch one asset into ``directory``, validating it before it is put in place.

    The download lands on a ``.part`` file and is renamed only once ``validate``
    accepts it, so an interrupted or corrupt transfer never leaves something
    that looks installable. An existing file of the right size that still
    validates is reused.
    """
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, asset.name)
    check = validate or (lambda _path: None)
    if reuse and os.path.exists(target) and (not asset.size or os.path.getsize(target) == asset.size):
        try:
            check(target)
            return target
        except FirmwareError:
            pass
    partial = f"{target}.part"
    written = 0
    with _urlopen(asset.url, token=token, opener=opener) as response, open(partial, "wb") as handle:
        while True:
            chunk = response.read(DOWNLOAD_CHUNK)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
            if progress:
                progress(written, asset.size)
        handle.flush()
        os.fsync(handle.fileno())
    if asset.size and written != asset.size:
        os.unlink(partial)
        raise FirmwareError(f"{asset.name}: downloaded {written} bytes, expected {asset.size}")
    try:
        check(partial)
    except FirmwareError:
        os.unlink(partial)
        raise
    os.replace(partial, target)
    return target


def download_release(release, directory, **kwargs):
    """Fetch a release's UF2 into ``directory``, rejecting anything that is not a valid image."""
    asset = release.firmware
    if asset is None:
        raise FirmwareError(f"release {release.tag} carries no .uf2 image")
    kwargs.setdefault("validate", inspect_uf2)
    return download_asset(asset, directory, **kwargs)


def _unescape(field):
    """Decode the octal escapes ``/proc/mounts`` uses for spaces and tabs."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def find_bootloader_volumes(mounts="/proc/mounts"):
    """Mount points of every mounted UF2 bootloader volume.

    Identified by the presence of :data:`BOOTLOADER_MARKER`, which the UF2 spec
    requires, rather than by a vendor-specific volume label.
    """
    found = []
    try:
        with open(mounts, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return found
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or not fields[2].startswith(("vfat", "msdos", "exfat")):
            continue
        point = _unescape(fields[1])
        if os.path.exists(os.path.join(point, BOOTLOADER_MARKER)):
            found.append(point)
    return found


def bootloader_info(mountpoint):
    """Parse a bootloader volume's ``INFO_UF2.TXT`` into a dict."""
    info = {}
    try:
        with open(os.path.join(mountpoint, BOOTLOADER_MARKER), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, sep, value = line.partition(":")
                if sep:
                    info[key.strip()] = value.strip()
    except OSError:
        return info
    return info


def volume_nodes(block, sysfs=SYS_BLOCK):
    """Partition nodes of a whole-disk device, or the disk itself when it has none.

    The RP2040 bootloader puts its FAT volume in a partition, so the disk node
    that sysfs reports for the mass-storage interface is not itself mountable --
    udisks rejects it as "not a mountable filesystem".
    """
    name = os.path.basename(block)
    parts = sorted(glob.glob(os.path.join(sysfs, name, f"{name}*")))
    return [f"/dev/{os.path.basename(part)}" for part in parts] or [block]


def find_bootloader_block(vid=BOOTLOADER_VID, pid=BOOTLOADER_PID):
    """Mountable volume node of an attached, possibly unmounted, RP2040 bootloader."""
    disk = next((d.block for d in find_devices(vid=vid, pid=pid) if d.block), None)
    return volume_nodes(disk)[0] if disk else None


def mount_bootloader(block=None, runner=subprocess.run):
    """Mount an unmounted bootloader volume with ``udisksctl``, returning its mount point.

    Flashing only needs a mounted volume; on a desktop the automounter has
    usually already provided one, and this is the fallback when it has not.
    """
    device = block or find_bootloader_block()
    if not device:
        raise FirmwareError("no UF2 bootloader block device is attached")
    command = ["udisksctl", "mount", "--no-user-interaction", "-b", device]
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except OSError as err:
        raise FirmwareError(f"cannot run udisksctl: {err}") from err
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"(?:Mounted .* at|already mounted at:?)\s*(\S.*?)\.?\s*$", output, re.MULTILINE)
    if match:
        return match.group(1)
    volumes = find_bootloader_volumes()
    if volumes:
        return volumes[0]
    hint = ""
    if "NotAuthorized" in output or "authentication agent" in output:
        hint = (
            f"; udisks needs an authorization this session cannot give. Mount it by hand"
            f" (sudo mount -o uid=$(id -u) {device} /mnt) and pass the mount point instead"
        )
    raise FirmwareError(f"udisksctl could not mount {device}: {output.strip()}{hint}")


def wait_for_bootloader(timeout=BOOTLOADER_TIMEOUT, interval=POLL_INTERVAL, sleep=time.sleep, mount=True):
    """Block until a UF2 bootloader volume is available, returning its mount point.

    When ``mount`` is set and the bootloader is attached but nothing has mounted
    it, one ``udisksctl`` attempt is made per poll.
    """
    deadline = time.monotonic() + timeout
    last = None
    while True:
        volumes = find_bootloader_volumes()
        if volumes:
            return volumes[0]
        if mount and find_bootloader_block():
            try:
                return mount_bootloader()
            except FirmwareError as err:
                last = err
        if time.monotonic() >= deadline:
            detail = f"; last mount attempt: {last}" if last else ""
            raise FirmwareError(f"no UF2 bootloader volume appeared within {timeout}s{detail}")
        sleep(interval)


def flash_uf2(uf2_path, mountpoint, chunk=DOWNLOAD_CHUNK, progress=None):
    """Copy a validated UF2 onto a mounted bootloader volume.

    The bootloader reboots the instant the last block lands, so the write, the
    flush and the close can all fail with the volume disappearing underneath
    them. Once every byte has been handed over that is the expected ending, not
    a failure; an error before then is real and propagates.
    """
    total = os.path.getsize(uf2_path)
    destination = os.path.join(mountpoint, os.path.basename(uf2_path))
    sent = 0
    try:
        with open(uf2_path, "rb") as source, open(destination, "wb") as sink:
            while True:
                block = source.read(chunk)
                if not block:
                    break
                sink.write(block)
                sent += len(block)
                if progress:
                    progress(sent, total)
            sink.flush()
            os.fsync(sink.fileno())
    except OSError as err:
        if sent < total:
            raise FirmwareError(f"writing {destination} failed after {sent}/{total} bytes: {err}") from err
    return sent


def wait_for_device(serial_number=None, timeout=REBOOT_TIMEOUT, interval=POLL_INTERVAL, sleep=time.sleep):
    """Block until a Videomancer re-enumerates after flashing, returning its ``DeviceInfo``."""
    deadline = time.monotonic() + timeout
    while True:
        for info in find_devices():
            if serial_number is None or info.serial_number == serial_number:
                return info
        if time.monotonic() >= deadline:
            raise FirmwareError(f"device did not re-enumerate within {timeout}s")
        sleep(interval)


def upgrade(
    spec="latest",
    shell=None,
    serial_number=None,
    directory=None,
    stable_only=False,
    force=False,
    token=None,
    releases=None,
    progress=None,
    report=None,
    volume=None,
    settle=5.0,
    sleep=time.sleep,
):  # pylint: disable=too-many-locals
    """Download a firmware release and flash it through the UF2 bootloader.

    Requires an open :class:`~pyvmancer.shell.ShellClient` to enter the
    bootloader; without one the unit must be put there by hand (hold BOOT while
    powering on) and ``shell`` left as ``None``. Returns a result dict
    describing what was flashed and the version the device reports afterwards.

    ``volume`` names an already-mounted bootloader volume, for when the mount
    cannot be made automatically -- a headless session where udisks has no way
    to authorize one, say. Otherwise the volume is discovered and mounted.

    Flashing a version the device already runs is skipped unless ``force``.
    ``report`` receives one-line progress strings.
    """
    say = report or (lambda _message: None)
    release = resolve_release(spec, releases=releases, stable_only=stable_only, token=token)
    current = None
    if shell is not None:
        current = shell.version()
        say(f"device reports {current}")
        if not force and current.strip() == release.version:
            return {
                "flashed": False,
                "reason": "already current",
                "version": current,
                "release": release.summary(),
            }
    directory = directory or os.path.join(os.path.expanduser("~"), ".cache", "pyvmancer", "firmware")
    say(f"fetching {release.firmware.name}")
    path = download_release(release, directory, token=token, progress=progress)
    image = inspect_uf2(path)
    say(f"validated {image.blocks} UF2 blocks, {image.payload_bytes} bytes of firmware")
    if shell is not None:
        if serial_number is None:
            serial_number = shell.serial_number()
        say("entering bootloader")
        shell.reboot_bootloader()
        shell.close()
    mountpoint = volume or wait_for_bootloader(sleep=sleep)
    say(f"bootloader mounted at {mountpoint}")
    flash_uf2(path, mountpoint, progress=progress)
    say("image written; waiting for the device to come back")
    sleep(settle)
    info = wait_for_device(serial_number=serial_number, sleep=sleep)
    return {
        "flashed": True,
        "previous": current,
        "release": release.summary(),
        "image": image.summary(),
        "path": path,
        "mountpoint": mountpoint,
        "serial": info.serial_number,
        "tty": info.tty,
        "version": confirm_version(info.serial_number, sleep=sleep),
    }


def confirm_version(serial_number=None, attempts=5, interval=2.0, sleep=time.sleep):
    """Reopen the serial link and read the running firmware version.

    Called straight after a flash, when the CDC interface may not have finished
    coming up, so the read is retried; ``None`` means the link never answered
    and the version has to be checked by hand.
    """
    from .device import open_shell

    for attempt in range(attempts):
        try:
            with open_shell(serial_number=serial_number) as shell:
                return shell.version()
        except Exception:
            if attempt == attempts - 1:
                return None
            sleep(interval)
    return None
