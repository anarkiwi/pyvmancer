"""Device discovery against a synthetic sysfs tree. No real device nodes are touched."""

import glob as glob_module
import os

import pytest

from pyvmancer import discovery
from pyvmancer.const import USB_PID, USB_VID
from pyvmancer.discovery import DeviceInfo, find_device, find_devices
from pyvmancer.errors import DeviceNotFoundError


def _write(path, text):
    """Create a sysfs-style attribute file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(name="sysfs")
def sysfs_fixture(tmp_path, monkeypatch):
    """A fake ``/sys/bus/usb/devices`` root, with /sys/class/sound and /dev redirected."""
    root = tmp_path / "sys" / "bus" / "usb" / "devices"
    root.mkdir(parents=True)
    monkeypatch.setattr(discovery, "_SYSFS_USB", str(root))
    real_glob = glob_module.glob

    def fake_glob(pattern, **kwargs):
        """Redirect absolute system lookups into the temporary tree."""
        for prefix in ("/sys/", "/dev/"):
            if pattern.startswith(prefix):
                return sorted(real_glob(str(tmp_path) + pattern, **kwargs))
        return real_glob(pattern, **kwargs)

    monkeypatch.setattr(discovery.glob, "glob", fake_glob)
    return root


def _make_device(sysfs, name="1-1", vid=USB_VID, pid=USB_PID, serial="VM0001", bus=3, address=42):
    """Populate one USB device directory and return its path."""
    entry = sysfs / name
    _write(entry / "idVendor", f"{vid:04x}\n")
    _write(entry / "idProduct", f"{pid:04x}\n")
    _write(entry / "serial", f"{serial}\n")
    _write(entry / "busnum", f"{bus}\n")
    _write(entry / "devnum", f"{address}\n")
    return entry


def _add_sound_card(tmp_path, entry, card=0, interface="1-1:1.0", midi=True):
    """Link an ALSA card at the USB interface of ``entry`` and create its rawmidi node."""
    (entry / interface).mkdir(parents=True, exist_ok=True)
    card_dir = tmp_path / "sys" / "class" / "sound" / f"card{card}"
    card_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(entry / interface, card_dir / "device")
    if midi:
        snd = tmp_path / "dev" / "snd"
        snd.mkdir(parents=True, exist_ok=True)
        (snd / f"midiC{card}D0").write_bytes(b"")


def test_no_devices_found(sysfs):
    """An empty sysfs tree yields no devices."""
    assert not list(sysfs.iterdir())
    assert find_devices() == []


def test_ignores_non_matching_and_incomplete_entries(sysfs):
    """Entries with a different VID/PID or no id attributes at all are skipped."""
    _make_device(sysfs, name="2-1", vid=0x1234)
    _make_device(sysfs, name="2-2", pid=0x9999)
    (sysfs / "usb1").mkdir()
    _write(sysfs / "3-1" / "idVendor", "16d0\n")
    assert find_devices() == []


def test_find_devices_reads_identity(sysfs):
    """Serial, bus and address come straight from sysfs attributes."""
    _make_device(sysfs)
    (device,) = find_devices()
    assert device.serial_number == "VM0001"
    assert (device.bus, device.address) == (3, 42)
    assert device.usbfs_path == "/dev/bus/usb/003/042"
    assert device.rawmidi is None
    assert device.tty is None
    assert device.block is None


def test_find_devices_locates_child_nodes(tmp_path, sysfs):
    """The tty, block and rawmidi nodes are derived from the device's interfaces."""
    entry = _make_device(sysfs)
    (entry / "1-1:1.2" / "tty" / "ttyACM0").mkdir(parents=True)
    (entry / "1-1:1.3" / "host0" / "target0:0:0" / "0:0:0:0" / "block" / "sda").mkdir(parents=True)
    _add_sound_card(tmp_path, entry)
    (device,) = find_devices()
    assert device.tty == "/dev/ttyACM0"
    assert device.block == "/dev/sda"
    assert device.rawmidi.endswith("/dev/snd/midiC0D0")


def test_alsa_card_belonging_to_another_device_is_ignored(tmp_path, sysfs):
    """A sound card under a different USB device is not claimed."""
    entry = _make_device(sysfs)
    other = _make_device(sysfs, name="9-9", vid=0x1234)
    _add_sound_card(tmp_path, other)
    assert find_devices()[0].rawmidi is None
    assert entry.exists()


def test_alsa_card_without_rawmidi_node(tmp_path, sysfs):
    """A matching card with no midi character device yields no rawmidi path."""
    entry = _make_device(sysfs)
    _add_sound_card(tmp_path, entry, midi=False)
    assert find_devices()[0].rawmidi is None


def test_invalid_numeric_attributes_become_none(sysfs):
    """Unreadable bus/address attributes leave the usbfs path undefined."""
    entry = _make_device(sysfs)
    _write(entry / "busnum", "not-a-number\n")
    (device,) = find_devices()
    assert device.bus is None
    assert device.usbfs_path is None


def test_find_device_filters_by_serial(sysfs):
    """The serial selector picks one of several attached devices."""
    _make_device(sysfs, name="1-1", serial="AAA")
    _make_device(sysfs, name="1-2", serial="BBB")
    assert len(find_devices()) == 2
    assert find_device("BBB").serial_number == "BBB"
    assert find_device().serial_number == "AAA"


def test_find_device_raises_when_nothing_matches(sysfs):
    """Nothing attached raises DeviceNotFoundError."""
    assert not list(sysfs.iterdir())
    with pytest.raises(DeviceNotFoundError, match="no Videomancer found$"):
        find_device()


def test_find_device_raises_for_unknown_serial(sysfs):
    """A serial that matches nothing names the serial in the error."""
    _make_device(sysfs, serial="AAA")
    with pytest.raises(DeviceNotFoundError, match="with serial 'ZZZ'"):
        find_device("ZZZ")


@pytest.mark.parametrize(
    "bus,address,expected",
    [(3, 42, "/dev/bus/usb/003/042"), (1, 1, "/dev/bus/usb/001/001"), (12, 345, "/dev/bus/usb/012/345")],
)
def test_usbfs_path_formatting(bus, address, expected):
    """usbfs paths are zero-padded to three digits."""
    assert DeviceInfo(bus=bus, address=address).usbfs_path == expected


@pytest.mark.parametrize("bus,address", [(None, 1), (1, None), (None, None)])
def test_usbfs_path_needs_both_fields(bus, address):
    """Without both bus and address there is no usbfs node."""
    assert DeviceInfo(bus=bus, address=address).usbfs_path is None


def test_device_info_repr():
    """The repr summarises identity and node paths."""
    info = DeviceInfo(serial_number="VM1", bus=1, address=2)
    info.rawmidi = "/dev/snd/midiC1D0"
    assert repr(info) == ("DeviceInfo(serial='VM1', rawmidi='/dev/snd/midiC1D0', tty=None, usb=1:2)")
