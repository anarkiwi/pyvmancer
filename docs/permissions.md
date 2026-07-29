# Device access and permissions

The Videomancer presents four device nodes. `vmancer devices` reports all of
them:

```json
{
  "serial": "E464B0605F113625",
  "rawmidi": "/dev/snd/midiC6D0",
  "tty": "/dev/ttyACM0",
  "block": "/dev/sdc",
  "usbfs": "/dev/bus/usb/003/041"
}
```

| Node | Used for | Default owner |
| --- | --- | --- |
| `/dev/snd/midiC*D*` | USB MIDI | `root:audio`, usually ACL-granted to the desktop user |
| `/dev/ttyACM*` | serial shell | `root:dialout` |
| `/dev/bus/usb/*/*` | serial shell over libusb | `root:root`, mode `0664` |
| `/dev/sd*` | microSD as mass storage | `root:disk` |

## Granting access

Serial normally just needs group membership, which takes effect on next login:

```sh
sudo usermod -aG dialout "$USER"
```

For a persistent, group-free rule, install a udev rule that ACLs every interface
of the device to the seat's active user:

```
# /etc/udev/rules.d/70-lzx-videomancer.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="16d0", ATTR{idProduct}=="14db", TAG+="uaccess"
SUBSYSTEM=="tty", ATTRS{idVendor}=="16d0", ATTRS{idProduct}=="14db", TAG+="uaccess"
```

```sh
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Containers

Passing the device into a container needs the nodes mapped *and* writable by the
container uid. Mapping `/dev/snd` alone gives you MIDI but not the shell.

```sh
docker run --device /dev/snd --device /dev/ttyACM0 ...
```

If `/dev/tty*` cannot be mapped, the serial shell can still be reached through
libusb against `/dev/bus/usb`, which needs read-write on the usbfs node:

```python
shell = pyvmancer.open_shell(prefer="usb")
```

Note that usbfs requires `O_RDWR`; read-only access is not enough to claim the
interface, and the node's address changes whenever the device re-enumerates.

Where `libasound.so.2` is absent, the pure-Python ALSA rawmidi backend still
works and needs no ALSA userspace at all:

```python
midi = pyvmancer.open_midi(prefer="rawmidi")
```

## Diagnosing

```sh
vmancer devices                  # what the library can see
amidi -l                         # ALSA's view of MIDI ports
cat /proc/asound/card*/midi0     # rawmidi Tx/Rx byte counters
```

The Tx counter is the quickest confirmation that bytes actually reached the
device. ALSA applies running-status compression, so the count is usually lower
than the number of bytes handed to the library.
