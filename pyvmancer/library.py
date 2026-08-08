"""The program library: published releases, and installing them onto the SD card.

LZX ships the FPGA program library as a zip on the same repository the firmware
comes from, tagged ``programs/<version>``. The archive mirrors the card's own
layout -- ``programs/lzx/<name>.vmprog`` plus ``programs/manifest.json`` -- so
installing it is a file copy onto ``sd:/``.

The copy goes over ``fs put``, the shell's raw streaming path. The base64
``fs write`` path carries about 96 bytes per command, which is hours for a
14 MB library; ``fs put`` announces a size and hands over raw bytes at roughly
157 kB/s, which is about a minute and a half.
"""

import hashlib
import os
import posixpath
import time
import zipfile

from .const import SHELL_PATH_PREFIX
from .errors import FirmwareError, TransportError, VmancerError
from .firmware import download_asset, fetch_bytes, resolve_release

#: Tag prefix selecting program library releases.
LIBRARY_PRODUCT = "programs"
#: Directory inside the archive that maps onto the card root.
ARCHIVE_ROOT = "programs/"
#: Files the library is allowed to install.
INSTALLABLE = (".vmprog", ".json")


def resolve_library(spec="latest", product=LIBRARY_PRODUCT, **kwargs):
    """Find the program library release named by ``spec``.

    Unlike the firmware, these releases are published as stable, so ``latest``
    picks up the newest without needing to opt into prereleases.
    """
    return resolve_release(spec, product=product, **kwargs)


def parse_checksums(text):
    """Parse a ``sha256sum`` sidecar into ``{filename: digest}``."""
    digests = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            digests[os.path.basename(parts[-1].lstrip("*"))] = parts[0].lower()
    return digests


def file_digest(path):
    """Streaming sha256 of a local file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _archive_check(expected):
    """Validator asserting a downloaded archive opens and matches ``expected``."""

    def check(path):
        if expected:
            actual = file_digest(path)
            if actual != expected:
                raise FirmwareError(f"{os.path.basename(path)}: sha256 {actual}, expected {expected}")
        try:
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise FirmwareError(f"{os.path.basename(path)}: archive is corrupt")
        except zipfile.BadZipFile as err:
            raise FirmwareError(f"{os.path.basename(path)}: not a zip archive") from err

    return check


def download_library(release, directory, token=None, opener=None, progress=None, reuse=True):
    """Fetch a library release's archive, verified against its published digest.

    The releases ship a ``checksums.sha256`` (older ones a ``<asset>.sha256``),
    so the archive is checked against the publisher's digest rather than only
    its length.
    """
    asset = release.archive
    if asset is None:
        raise FirmwareError(f"release {release.tag} carries no .zip archive")
    expected = None
    if release.checksums is not None:
        sidecar = fetch_bytes(release.checksums, token=token, opener=opener).decode("utf-8", "replace")
        expected = parse_checksums(sidecar).get(asset.name)
    return download_asset(
        asset,
        directory,
        token=token,
        opener=opener,
        progress=progress,
        reuse=reuse,
        validate=_archive_check(expected),
    )


class LibraryEntry:
    """One file the archive installs, and where it lands on the card."""

    __slots__ = ("name", "device_path", "size")

    def __init__(self, name, device_path, size):
        self.name = name
        self.device_path = device_path
        self.size = size

    def __repr__(self):
        return f"LibraryEntry(device_path={self.device_path!r}, size={self.size})"


def _device_path(name):
    """Map an archive member onto its ``sd:/`` path, rejecting anything that escapes."""
    normalised = posixpath.normpath(name)
    if normalised.startswith(("/", "../")) or normalised == ".." or "\\" in name:
        raise FirmwareError(f"archive member escapes the card root: {name!r}")
    return SHELL_PATH_PREFIX + normalised


def library_entries(path, root=ARCHIVE_ROOT):
    """Every installable file in an archive, as :class:`LibraryEntry` objects.

    Members outside ``root`` and file types the card has no use for are skipped,
    so a release that grows a README does not try to install it.
    """
    entries = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith(root):
                continue
            if not info.filename.lower().endswith(INSTALLABLE):
                continue
            entries.append(LibraryEntry(info.filename, _device_path(info.filename), info.file_size))
    return sorted(entries, key=lambda entry: entry.device_path)


def installed_sizes(shell, entries):
    """Size of each entry already on the card, keyed by device path.

    Read with one listing per directory rather than one ``fs stat`` per file,
    which matters when the library is 40 files deep.
    """
    sizes = {}
    for directory in sorted({posixpath.dirname(e.device_path) for e in entries}):
        try:
            listing = shell.listdir(directory)
        except VmancerError:  # the directory does not exist yet
            continue
        for item in listing:
            if item.get("type") == "file":
                sizes[f"{directory}/{item.get('name')}"] = item.get("size")
    return sizes


def _ensure_directories(shell, entries):
    """Create the card directories the entries need.

    ``fs mkdir`` on a directory that already exists is an error rather than a
    no-op, and the common case is that every one of them already exists.
    """
    made = set()
    for directory in sorted({posixpath.dirname(e.device_path) for e in entries}):
        if directory.rstrip("/") == SHELL_PATH_PREFIX.rstrip("/"):
            continue
        try:
            shell.mkdir(directory)
            made.add(directory)
        except VmancerError:
            pass
    return made


def install_library(
    shell,
    path,
    force=False,
    report=None,
    progress=None,
    verify=True,
    reconnect=None,
    attempts=6,
):  # pylint: disable=too-many-locals
    """Upload an archive's files onto the card, returning a per-file result.

    Files already present at the archive's size are skipped unless ``force``.
    The device's own ``fs hash`` would be the cheap way to compare content, but
    it returns wrong digests on ``1.0.0-rc.46``, so size is the only cheap
    signal available and ``verify`` re-reads the size the card reports after
    each upload.

    Sustained puts drop the USB link: the device re-enumerates part way through
    a full library, which surfaces as an ``EIO`` write and a dead handle. It
    comes back on its own, so ``reconnect`` is called for a fresh shell and the
    upload carries on from the file that was in flight. That file is always
    re-sent -- an interrupted put can leave the declared size on the card with
    nothing but partial content behind it, so its size proves nothing.
    """
    say = report or (lambda _message: None)
    entries = library_entries(path)
    if not entries:
        raise FirmwareError(f"{os.path.basename(path)} contains no installable files")
    existing = {} if force else installed_sizes(shell, entries)
    _ensure_directories(shell, entries)
    installed, skipped = [], []
    total = sum(e.size for e in entries)
    done = 0
    drops = 0
    with zipfile.ZipFile(path) as archive:
        pending = list(entries)
        while pending:
            entry = pending[0]
            if entry.device_path not in installed and existing.get(entry.device_path) == entry.size:
                skipped.append(entry.device_path)
                done += entry.size
                pending.pop(0)
                continue
            payload = archive.read(entry.name)
            if len(payload) != entry.size:
                raise FirmwareError(f"{entry.name}: archive declares {entry.size}, holds {len(payload)}")
            try:
                shell.put_file(entry.device_path, payload)
                if verify:
                    landed = int(shell.stat(entry.device_path).get("size", -1))
                    if landed != entry.size:
                        raise FirmwareError(f"{entry.device_path}: card holds {landed} of {entry.size}")
            except TransportError as err:
                drops += 1
                if reconnect is None or drops >= attempts:
                    raise FirmwareError(
                        f"{entry.device_path}: link lost after {done} of {total} bytes ({err}); "
                        f"re-run to resume from here"
                    ) from err
                say(f"link dropped on {entry.device_path}; reconnecting and resuming")
                shell = reconnect()
                continue
            installed.append(entry.device_path)
            done += entry.size
            pending.pop(0)
            say(f"installed {entry.device_path} ({entry.size} bytes)")
            if progress:
                progress(done, total)
    return {"installed": installed, "skipped": skipped, "bytes": total, "link_drops": drops}


def install(
    spec="latest",
    shell=None,
    serial_number=None,
    directory=None,
    force=False,
    token=None,
    releases=None,
    progress=None,
    report=None,
    verify=True,
    reconnect=None,
):
    """Download a program library release and install it onto the card.

    Returns a result dict naming the release, what was uploaded and what was
    already present at the right size. ``reconnect`` overrides how the shell is
    reopened when the device drops the link mid-upload; the default reopens the
    same unit by serial number.
    """
    say = report or (lambda _message: None)
    if shell is None:
        raise FirmwareError("installing the program library needs an open serial shell")
    release = resolve_library(spec, releases=releases, token=token)
    directory = directory or os.path.join(os.path.expanduser("~"), ".cache", "pyvmancer", "programs")
    say(f"fetching {release.archive.name}")
    path = download_library(release, directory, token=token, progress=progress)
    say("archive verified against its published sha256")
    result = install_library(
        shell,
        path,
        force=force,
        report=say,
        progress=progress,
        verify=verify,
        reconnect=reconnect or _reopener(serial_number),
    )
    if result["installed"]:
        say("restart the device: the program index is built at boot, so new programs appear then")
    return {
        "release": release.summary(),
        "path": path,
        "restart_required": bool(result["installed"]),
        **result,
    }


def _reopener(serial_number=None, settle=3.0, attempts=10, interval=1.0, sleep=time.sleep):
    """Build a callable that waits for the device to re-enumerate and reopens the shell."""

    def reopen():
        from .device import open_shell

        sleep(settle)
        last = None
        for _ in range(attempts):
            try:
                return open_shell(serial_number=serial_number)
            except VmancerError as err:
                last = err
                sleep(interval)
        raise FirmwareError(f"device did not come back after the link dropped: {last}")

    return reopen
