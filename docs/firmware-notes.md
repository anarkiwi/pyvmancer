# Firmware notes

Observations from a Videomancer running `1.0.0-rc.37` and `1.0.0-rc.40`.
The published LZX documentation describes a `2.1.0` firmware, and the two differ
in several places. Always treat `help` and `fs caps` as authoritative.

## `settings export` resets the USB link

Running `settings export` reliably drops the CDC connection mid-reply:

```
TransportError: read from /dev/ttyACM0 failed: device reports readiness to read
but returned no data
```

The device then re-enumerates with a new USB address; the next write on the old
handle fails with `EIO`. The unit recovers on its own and no state is lost, but
any open handle must be reopened, and a `/dev/bus/usb` ACL granted to the old
address is gone.

The reply appears to exceed the shell's response buffer. `pyvmancer` therefore
does not wrap `settings export`; use `settings get <key_id>` for individual keys.

## Error codes are tagged

Documented codes are 1-10. This firmware returns them as `0x58000000 | n`, so a
usage error arrives as `1476395010`. `pyvmancer.shell.decode_error_code` strips
the tag; `ShellError.code` is the decoded value.

## Command set is much larger than documented

`help` reports 64 commands against the ~30 in the published guide. The additions
that matter for automation are the whole `modulation` group (`status`, `source`,
`set`, `reset`, `cc-map`, `latch`, `audio status`), `program info`, the `video`
and `remote` groups, and `settings get`/`set`/`reset`.

## Not every command acknowledges with `ok`

Most commands reply `@<tag>:ok`, but several use a verb of their own. Observed on
`1.0.0-rc.37`:

| Command | Reply |
| --- | --- |
| `remote enable` | `enabled` |
| `remote disable` | `disabled` |
| `midi monitor <mode>` | `monitor <mode>` |
| `log level <level>` | the level, uppercased, e.g. `INFO` |
| `language` (no argument) | the current locale, e.g. `en_US` |

`transport play/stop/bpm`, `modulation set/source/reset/latch`, `remote write` and
the preset and filesystem mutations all do reply `ok`.

## `fs read` caps the base64 payload, not the decoded bytes

`fs caps` reports `read_max_bytes: 256` on `1.0.0-rc.37` and `rc.40`, and the
reply is a JSON envelope, `{"data":"<base64>","read":n}`. The limit applies to
the encoded payload: asking for 256 decoded bytes produces 344 characters of
base64, overruns the buffer and truncates the reply. Requests must ask for at
most `read_max_bytes // 4 * 3` decoded bytes; 96 bytes per request was verified
working on hardware, and `pyvmancer.shell.decoded_limit` derives the maximum.

Both halves of this were wrong in 0.1.0: the whole envelope was base64-decoded,
and the chunk size was in the wrong units, so no file read back correctly.

## `fs hash` is the cheap way to key a cache, but it is not fast

`fs caps` advertises `fs_hash: 1`, and `fs hash <path>` returns
`{"hash": "<sha256 hex>", "size": n}` computed on the device, without moving the
file over the 256-byte-per-chunk read path.

It hashes at about **79 kB/s**, so the reply routinely takes longer than the
shell client's 5.0s default timeout:

| File | Size | Time | Rate |
| --- | --- | --- | --- |
| `manifest.json` | 16526 B | 0.21 s | 77.0 kB/s |
| `blizzard.vmprog` | 322934 B | 3.98 s | 79.3 kB/s |
| `combing.vmprog` | 439221 B | 5.40 s | 79.4 kB/s |

Program binaries run 320-440 kB, so most exceed a 5-second deadline and the
largest exceed it every time; only the small manifest fits.
`ShellClient.hash_file` therefore derives its timeout from the size `fs stat`
reports, at `HASH_BYTES_PER_SECOND` with `HASH_TIMEOUT_FACTOR` of headroom for a
slower card, and still accepts an explicit `timeout=`.

Commands whose payload is chunked need none of this: `fs read` and `fs write`
each carry at most a few hundred bytes, so they finish well inside the default.

## `fs put` is the only usable upload path, and it owns the link

`fs write` carries base64 inside the command line, so it moves about 96 bytes
per command — roughly 3 kB/s, or days for a 14 MB program library. `fs put` is
the alternative: `fs put <path> <size>` answers `{"put":"ready","size":n}`, the
device then takes exactly `size` **raw** bytes off the link, and answers
`{"put":"ok","written":n}`. Measured at 157-166 kB/s, byte-exact on a payload
covering every byte value.

The hazard is that between `ready` and the final byte the device treats
*everything* arriving as file content. A client that gives up mid-payload leaves
the device consuming subsequent commands as data: `fs stat` gets no reply, and
the only ways out are feeding it the promised byte count or a power cycle. Two
things make that easy to trigger, both fixed in `pyvmancer`:

- **The device stalls while committing to the card**, past pyserial's old 2.0s
  `write_timeout`, when a program-sized payload lands in a directory that already
  holds the library. Root-directory puts never stalled. The transports now allow
  `serial_tty.WRITE_TIMEOUT` (30s) for a blocked write.
- **Sustained puts can drop the USB link.** One run re-enumerated after about
  7 MB, surfacing as `EIO`. The unit recovers on its own, so
  `library.install_library` reconnects and resumes; a later full run of the same
  14 MB completed with no drops, so it is intermittent rather than a hard limit.

Note also that a bare `fs put` or `fs write` with no arguments answers
`unknown fs subcommand`, which reads like the command is missing. Both exist;
the message is just a poor usage error.

## `fs hash` returns wrong digests on `1.0.0-rc.46`

Checked against the published program archive as ground truth: `fs read` returns
bytes matching the upstream file exactly, while `fs hash` reports a different
sha256 for the same path, with the same size. Both `manifest.json` (2918 B) and
`bleach.vmprog` (318786 B) disagreed.

So the cheap cache key described above is not trustworthy on this firmware, and
`ShellClient.hash_file` should not be relied on until it is confirmed fixed.
`library.install_library` compares sizes and re-reads the landed size instead.

## `fs ls` pages five at a time and repeats the last entry

A full page arrives as `{"entries":[...],"more":true,"next":n}`; the final short
page arrives as a bare array whose last entry is duplicated. A root listing with
one directory in it returns that directory twice. `ShellClient.listdir` pages
and de-duplicates by name; `ls` still returns the raw reply.

## The program index is built at boot

Programs installed onto the card do not appear in `programs list` and cannot be
loaded — `program load` answers `[6] program not found` — until the unit is
restarted. Observed with a program whose file was byte-identical to the
published archive while a neighbouring one, present at the previous boot, loaded
fine. There is no software restart short of `reboot bootloader`, so installing
programs means power cycling afterwards.

## The program manifest covers only SD-installed programs

`sd:/programs/manifest.json` carries the name, id, version, categories, type,
description and author of each program installed on the card. Firmware built-ins
are absent from it, so `programs list` reports more programs than the manifest
describes, and no description exists anywhere for the remainder.
`ProgramManifest.missing` names the difference for a given device listing.

## Modulation knobs need an active source

`modulation set <index> <manual> <time> <space> <slope>` accepts and acknowledges
all five arguments even when the slot's source is `Disabled`, but `t`/`sp`/`sl`
in `program state` stay at 512. Assign a source first, then set the knobs.

## Manual value and MIDI are separate

A MIDI CC does not change `m` in `program state`. It contributes to the combined
`o` output in `modulation status` while leaving the stored manual value alone,
matching the documented `Manual + Modulation + MIDI` sum. To change the stored
value, use `modulation set` over serial.

## Absolute addressing needs a parked reference

Because a CC is an offset, the same CC value means different things depending on
where the manual values happen to sit. Driving every manual value to a known
reference makes CC addressing absolute, and is the precondition for any
repeatable automation; `Videomancer.park` does it. Two behaviours it has to work
around, both observed on hardware:

- **Writes must be paced.** An unpaced burst of 24 messages drops the LSB of
  14-bit CC pairs, leaving MSB-only values such as `128`. `park` paces at a
  documented `rate_hz`.
- **The readback must settle.** Serial and MIDI are asynchronous, so the last
  slot written is otherwise sampled before the device has applied it. `park`
  waits, then polls, and only then compares.

The reference itself must leave P12 open: P12 is the crossfader and gates the
output even in programs whose `program info` names it `Null 12`, so parking it
at zero blacks the device out entirely and every subsequent measurement is
degenerate. `pyvmancer.const.PARK_REFERENCE` zeroes P1-P11 and opens P12.

## `video status` flags are advisory

The top-level `locked` flag tracks **genlock**, so it reads false whenever the
timing is overridden even though the input is fine. The selected input's own
sub-status (`hdmi`/`analog`) is authoritative in both directions, which is what
`VideoStatus.source_locked` reads.

Both are still only advisory. The firmware reports `hdmi.connected: false` while
passing video perfectly, and reports a locked input while passing nothing at
all. Nothing the device says proves frames are arriving; only observing the
output does, which is the caller's job — `pyvmancer` has no capture path.

## Recovering an output that stopped passing frames

Observed repeatedly: the input stops passing video while the device still
reports the input locked. There is no reboot verb short of `reboot bootloader`,
which enters the flashing state rather than restarting.

| Recovery attempt | Disturbs the pipeline | Recovers |
| --- | --- | --- |
| `modulation reset` | no | - |
| `video input <same>` | no | - |
| `video timing <native>` | no | - |
| `video timing <other>` | yes | no, the output dies |
| `video timing <other>` then `<native>` | yes | yes |

Bouncing the timing to a standard the genlocked source cannot satisfy and then
back re-initialises the output raster. `ShellClient.resync` performs it. An
external capture measurement of one such recovery took output motion from
`0.00000` to `0.02802` with no power cycle; that figure came from a capture card
on one rig and nothing in `pyvmancer` depends on it.

Three consequences. Which alternate standard to bounce through is a property of
the source and sink, not of the device, so `resync` discovers the accepted set
from the `video timing` usage string and takes an `alternate` argument. A timing
change also drops the input selection, so it is reasserted afterwards. And the
lock flags lag the signal, so the result is polled rather than sampled once.

The bounce leaves `overridden: true`: the device no longer follows a source
format change until the timing is set back or the device is restarted.

## Program inventory

21 programs on this unit, several of which are not in the published list
(`Colorbars`, `Passthru`), and some published names are absent (`Fauxtress`,
`Moire`, `Mycelium`, `Perlin`). `pyvmancer.const.EMBEDDED_PROGRAMS` is a
convenience list only; call `programs list`.

## Upgrades go through an RP2040 UF2 bootloader

`help` advertises no firmware-update verb; the only route is `reboot bootloader`,
which does not restart the device but drops it into the flashing state. The
published images confirm what that state is: the UF2 header of
`videomancer-1.0.0-rc.46.uf2` carries family id `0xe48bff56` (RP2040) at flash
base `0x10000000`, 47774 blocks for 12230144 bytes of payload in a 24460288-byte
file. So the unit re-enumerates as a standard RP2040 BOOTSEL mass-storage volume
and the image is installed by copying it there.

Two consequences for anything automating this. The bootloader reboots the
instant the last block lands, so the write, the flush and the close can all fail
with the volume vanishing underneath them — once every byte is delivered that is
the expected ending, not an error, which is what `firmware.flash_uf2` encodes.
And the volume is identified by the UF2 spec's `INFO_UF2.TXT` marker rather than
by an `RPI-RP2` label, so a relabelled or vendor-customised bootloader still
matches.

Verified end to end on hardware, rc.40 to rc.46. Three things that only showed up
against the real bootloader:

- **The FAT volume is a partition.** The mass-storage interface reports
  `/dev/sdc`, but the `RPI-RP2` volume is `/dev/sdc1`; udisks rejects the disk
  node with "not a mountable filesystem". `firmware.volume_nodes` resolves the
  partition from sysfs.
- **The copy is not the flash.** Writing to a page-cached vfat mount returns long
  before the data reaches the device: the file listed at its full 24460288 bytes
  while the unit was still enumerated as `2e8a:0003 RP2 Boot`. The transfer
  completes at `fsync`, which takes minutes for a 24 MB image, and the device
  reboots only then. Anything driving this needs a timeout in minutes, and the
  proof of a successful flash is the version read back afterwards, not the write
  returning.
- **Mounting needs an authorization a headless session lacks.** udisks answers
  `NotAuthorizedCanObtain` with no way to prompt. `upgrade(volume=...)` /
  `--volume` takes a mount point made by other means.

## Every published Videomancer release is a prerelease

`lzxindustries/videomancer-firmware` carries several products — `videomancer`,
`tbc2`, `diver`, `connect`, `programs` — tagged `<product>/<version>`. All 34
Videomancer releases are flagged `prerelease: true` on GitHub, so the repository's
"latest release" endpoint never returns one; it answers with whichever other
product most recently shipped a stable build. Videomancer releases have to be
selected by tag prefix and ordered by semver precedence, which is also what keeps
`1.0.0-rc.46` ahead of `1.0.0-rc.9`. `firmware.resolve_release` does both.

## MIDI byte counts

`/proc/asound/card*/midi0` Tx counts are lower than the bytes handed to the
library because the ALSA sequencer applies running-status compression. A
high-resolution parameter write plus a program change plus a note pair measured
21 bytes rather than 26.
