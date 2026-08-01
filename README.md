# pyvmancer

Python library and CLI to automate the [LZX Industries Videomancer](https://lzxindustries.net/products/videomancer) over USB.

Two control paths, usable together or independently:

| Path | Transport | Covers |
| --- | --- | --- |
| USB MIDI | ALSA rawmidi or any `mido` backend | 12 parameters (7/14-bit CC), preset recall, note triggers, clock/transport |
| USB serial | `/dev/ttyACM*` or libusb | programs, presets, modulation operators, video, settings, filesystem |

## Install

```sh
pip install pyvmancer            # MIDI only
pip install "pyvmancer[all]"     # adds serial and libusb transports
```

On Linux the `python-rtmidi` dependency links `libasound.so.2`:

```sh
sudo apt-get install libasound2t64
```

## Use

```python
import pyvmancer as vm

d = vm.Videomancer.open()

d.load_program("Isotherm")
d.set_named("Posterize", 5)        # native units: Posterize is 0..7
d.set_named("Mix", 80)             # Mix is 0..100
d.set_source("Contrast", "free lfo")

d.set_param(1, 0.75)               # or address slots directly, 0.0..1.0
d.set_param(7, True)               # toggles take bools
d.trigger(4)                       # note-trigger a modulation operator

d.set_bpm(128)
d.play()
d.close()
```

Device knowledge the library encodes, rather than leaving to each caller:

```python
d.park()                           # manual values to a reference, so a CC is absolute
p = d.parameter("Posterize")       # p.role, p.steps, p.sample_values()
d.program_manifest().get("combing").description
d.file_hash("sd:/programs/lzx/combing.vmprog")
d.video_state().source_locked      # per-input lock, not the genlock flag
d.resync()                         # bounce the timing to re-init the output raster
```

MIDI only, no serial link needed:

```python
with vm.open_midi(channel=1) as m:
    m.set_param(1, 0.5)
    m.select_preset(3)
    m.run_clock(120, beats=8)
```

## CLI

```sh
vmancer devices                 # discover attached units and their device nodes
vmancer operators               # list the 25 modulation operators
vmancer set 1=0.5 7=on          # set parameters over MIDI
vmancer preset 3                # recall a preset
vmancer programs                # list installed FPGA programs
vmancer load Isotherm           # load a program
vmancer manifest                # SD program library metadata
vmancer hash sd:/programs/x.vmprog
vmancer resync                  # recover an output that stopped passing frames
vmancer shell modulation status # run any raw shell command
```

## Permissions

MIDI needs read/write on `/dev/snd/midiC*D*` (usually granted by a desktop ACL);
serial needs the `dialout` group or an ACL on `/dev/ttyACM*`. See
[docs/permissions.md](docs/permissions.md).

## Docs

- [docs/protocol.md](docs/protocol.md) — MIDI implementation and the serial command set
- [docs/permissions.md](docs/permissions.md) — device node access, containers, udev
- [docs/firmware-notes.md](docs/firmware-notes.md) — observed firmware behaviour and quirks
- [CHANGELOG.md](CHANGELOG.md) — release history

## Status

Verified against a Videomancer running firmware `1.0.0-rc.37` and `1.0.0-rc.40`.
Not affiliated with LZX Industries.

## License

Apache-2.0
