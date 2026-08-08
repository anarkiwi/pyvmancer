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

## Firmware upgrade

LZX publishes firmware as GitHub releases on
[`lzxindustries/videomancer-firmware`](https://github.com/lzxindustries/videomancer-firmware),
tagged `videomancer/<version>`. `pyvmancer` resolves a release, downloads its
UF2, validates it, and flashes it through the RP2040 bootloader.

```sh
vmancer firmware list                  # published releases, newest first
vmancer firmware status                # running version vs the newest release
vmancer firmware download 1.0.0-rc.46  # fetch and validate a UF2, no flashing
vmancer firmware upgrade               # dry run: shows what would be flashed
vmancer firmware upgrade --yes         # flash the latest release
vmancer firmware upgrade 1.0.0-rc.40 --yes --force   # pin a version, or downgrade
```

`upgrade` is a dry run unless `--yes` is given. With `--yes` it issues
`reboot bootloader`, waits for the UF2 volume to appear (mounting it with
`udisksctl` if nothing automounted it), copies the image, then waits for the
device to re-enumerate and reports the version it comes back on. Pass
`--manual` when the unit was put into bootloader mode by hand (hold BOOT while
powering on), which is the recovery path when the serial link is unusable, and
`--volume` when the bootloader is already mounted somewhere.

Allow minutes, not seconds: the image only reaches the device when the copy is
flushed, and a 24 MB UF2 takes a while over a FAT mount. The device reboots
itself once the last block lands, and the version it reports afterwards is the
proof the flash took.

```python
import pyvmancer as vm

vm.resolve_release("latest").version            # newest published version
image = vm.inspect_uf2("videomancer-1.0.0-rc.46.uf2")
image.blocks, image.families                    # validated before anything is written

with vm.open_shell() as shell:
    vm.upgrade("latest", shell=shell, report=print)
```

Every Videomancer release to date is flagged as a prerelease on GitHub, so
`latest` includes prereleases; pass `--stable` / `stable_only=True` to refuse
them. Set `GITHUB_TOKEN` if you hit the unauthenticated API rate limit.

## Program library

The FPGA programs ship separately from the firmware, tagged `programs/<version>`
on the same repository. `pyvmancer` resolves a release, verifies the archive
against its published `checksums.sha256`, and copies it onto the SD card.

```sh
vmancer library list               # published library releases
vmancer library status             # newest release vs what is on the card
vmancer library download 1.0.3     # fetch and verify an archive, no install
vmancer library install            # dry run: shows what would be installed
vmancer library install --yes      # install the latest release
vmancer library install --yes --force   # re-upload files already present
```

```python
import pyvmancer as vm

with vm.open_shell() as shell:
    vm.install("latest", shell=shell, report=print)
```

Files already on the card at the archive's size are skipped, so an interrupted
run resumes by re-running it. The upload uses `fs put`, the shell's raw
streaming path: about 157 kB/s, so a full 14 MB library takes about 90 seconds.
The base64 `fs write` path manages roughly 3 kB/s and is only for small files.

**Restart the device after installing.** The program index is built at boot, so
newly installed programs are not listed — and cannot be loaded — until the unit
is power cycled.

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
