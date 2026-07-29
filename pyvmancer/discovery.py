"""Locate connected Videomancers and the device nodes they expose."""

import glob
import os
import re

from .const import USB_PID, USB_VID
from .errors import DeviceNotFoundError

_SYSFS_USB = "/sys/bus/usb/devices"
_CARD_ID_RE = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]")


class DeviceInfo:
    """Device nodes and identity for one attached Videomancer."""

    __slots__ = ("serial_number", "usb_path", "bus", "address", "rawmidi", "tty", "block")

    def __init__(self, serial_number=None, usb_path=None, bus=None, address=None):
        self.serial_number = serial_number
        self.usb_path = usb_path
        self.bus = bus
        self.address = address
        self.rawmidi = None
        self.tty = None
        self.block = None

    def __repr__(self):
        return (
            f"DeviceInfo(serial={self.serial_number!r}, rawmidi={self.rawmidi!r}, "
            f"tty={self.tty!r}, usb={self.bus}:{self.address})"
        )

    @property
    def usbfs_path(self):
        """Path of the usbfs node, or ``None`` if the bus/address are unknown."""
        if self.bus is None or self.address is None:
            return None
        return f"/dev/bus/usb/{self.bus:03d}/{self.address:03d}"


def _read(path):
    """Read and strip a sysfs attribute, returning None when absent."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _int(path):
    """Read a sysfs attribute as int, returning None when absent or invalid."""
    value = _read(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _alsa_card_for(usb_path):
    """Map a sysfs USB device path to its ALSA rawmidi node.

    A card's ``device`` link points at the USB *interface* that carries audio,
    so the match is against that interface's parent device.
    """
    target = os.path.realpath(usb_path)
    for control in glob.glob("/sys/class/sound/card*/device"):
        if os.path.dirname(os.path.realpath(control)) != target:
            continue
        card = os.path.basename(os.path.dirname(control)).removeprefix("card")
        nodes = sorted(glob.glob(f"/dev/snd/midiC{card}D*"))
        if nodes:
            return nodes[0]
    return None


def _tty_for(usb_path):
    """Find the tty node exposed by the CDC interface of a USB device."""
    for tty_dir in glob.glob(os.path.join(usb_path, "*", "tty", "*")):
        return f"/dev/{os.path.basename(tty_dir)}"
    return None


def _block_for(usb_path):
    """Find the block device exposed by the mass-storage interface."""
    for block_dir in glob.glob(os.path.join(usb_path, "*", "host*", "target*", "*", "block", "*")):
        return f"/dev/{os.path.basename(block_dir)}"
    return None


def find_devices(vid=USB_VID, pid=USB_PID):
    """Enumerate attached Videomancers by scanning sysfs.

    Returns an empty list on platforms without sysfs; use the transport classes
    directly there.
    """
    found = []
    for entry in sorted(glob.glob(os.path.join(_SYSFS_USB, "*"))):
        vendor = _read(os.path.join(entry, "idVendor"))
        product = _read(os.path.join(entry, "idProduct"))
        if vendor is None or product is None:
            continue
        if int(vendor, 16) != vid or int(product, 16) != pid:
            continue
        info = DeviceInfo(
            serial_number=_read(os.path.join(entry, "serial")),
            usb_path=entry,
            bus=_int(os.path.join(entry, "busnum")),
            address=_int(os.path.join(entry, "devnum")),
        )
        info.rawmidi = _alsa_card_for(entry)
        info.tty = _tty_for(entry)
        info.block = _block_for(entry)
        found.append(info)
    return found


def find_device(serial_number=None, vid=USB_VID, pid=USB_PID):
    """Return the single matching Videomancer, or raise :class:`DeviceNotFoundError`."""
    devices = find_devices(vid=vid, pid=pid)
    if serial_number is not None:
        devices = [d for d in devices if d.serial_number == serial_number]
    if not devices:
        detail = f" with serial {serial_number!r}" if serial_number else ""
        raise DeviceNotFoundError(f"no Videomancer found{detail}")
    return devices[0]
