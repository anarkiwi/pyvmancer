# Firmware notes

Observations from a Videomancer running `1.0.0-rc.37`, `1.0.0-rc.40` and `1.0.1`,
beyond what the [LZX Serial Port Guide](https://lzxindustries.net/instruments/videomancer/manual/serial-command-guide) documents. Treat `help` and `fs caps` as authoritative
for the connected unit.

## `settings export` resets the USB link (`rc.37`)

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

## `1.0.1`

- **The front-panel route can hold the output timing.** With SETTINGS > Vid Route
  Mode or SYSTEM > Pref. Timing resolving away from the selected input, the output
  stays at that timing with `overridden: true` while the input is locked at a
  different one, e.g. output `NTSC` under an HDMI input locked at `1080p30`.
  `video timing 1080p30` then acknowledges `ok` and changes nothing. Setting Vid
  Route Mode to the input in use (or Auto), Pref. Timing to the source format, and
  power-cycling clears it. `ShellClient.apply_video_timing` reads the output back
  and raises `VideoTimingIgnored`; `vmancer doctor` reports it as `video.genlock`.
- **`hdmi_tx` shows whether the transmitter drives video.** `vic: 0` with
  `de: "000000"` means no active video leaves the HDMI output, even with
  `output.hdmi_connected: true`; this followed the source HDMI link powering down.
  `vmancer doctor` reports it as `video.output_active`.
- **CC writes apply within a frame and do not back up.** At 1080p30, a serial
  `combined_values()` round trip took about 2 ms, and a single CC write read back
  converged in about 33 ms, one frame. Ramping one parameter at 80 sets/s, or six
  at 40 sets/s (about 480 CC messages/s), left no backlog: the last write still
  converged within a frame. `vmancer latency` measures this; `vmancer doctor
  --latency` fails `midi.latency` above two frames plus the readback round trip,
  and `midi.backlog` when a ramp's last write lags a single write by over a frame.
- **`video timing` usage names no standards.** A bare `video timing` answers
  `[1] video timing`, so `video_timings()` is empty and `resync` needs an explicit
  `alternate`.
- **The shell blocks during device-side work and answers nothing meanwhile.** On
  `1.0.1`, traced with `fpga status` polled every 100 ms: `programs enumerate lzx`
  took about 6 s and `programs enumerate czartroniks` about 10.7 s; `program load`
  answered within about 1 s once its author was active, and 0.02 s between two lzx
  programs. A raw `program load Fray` with lzx active answered only after 11.4 s,
  with queued polls unanswered and no log output, while the device stayed on USB.
  The FPGA can then report `state idle, configured true, program ''` or keep
  running the previous program before reaching the new one, and one `fpga status`
  poll can itself go unanswered while it configures. None of this is a fault, so
  these commands are awaited on liveness (below), not a deadline. A fixed deadline
  (20 s in one client) once failed a Passthru load that completed on its own.
- **`program load` is asynchronous.** Its `ok` can arrive before the FPGA
  reconfiguration completes, so an immediate `program info` answers `[3] no program
  loaded`. Seen when an archive run loaded Passthru twice in succession: the second
  load's `program info` failed, while the device ran Passthru normally moments later. `fpga status` reports `state`,
  `configured` and `program`; `ShellClient.load_program` polls it until the requested
  program is `running` and `configured` and `program info` answers. Preset recalls (`Videomancer.apply_preset`, `select_preset`) wait the same
  way before the CC map is assigned.
- **`settings export` and `fs read` keep the USB link up**, unlike `rc.37` below.
  Settings keys are numeric only.

## CC map

Observed on `1.0.1`.

- **Encoding.** `modulation cc-map` lists one `{"msb", "lsb", "auto"}` pair per
  slot, or `null` when unassigned. Settings keys 200-211 (P1-P12) persist the map:
  each holds `0x1FF00 | msb`, or `0x1FFFF` unassigned, e.g. `130816` for CC 0 and
  `130827` for CC 11, read as `{"key":211,"type":"uint32","value":130827}`. Key 199
  answers `setting not found`. An MSB of 0-31 reports `lsb: msb + 32`; an MSB of 32
  or above (50, 96 and 191 were tried) reports `lsb: 255`, i.e. 7-bit only.
  `modulation cc-map` takes no arguments; the settings keys are the only remote
  write path. `assign_cc_map` refuses to write when any key falls outside this
  encoding, since other firmware may encode differently.
- **Same-value writes are ignored, and the keys can disagree with the live map.**
  With key 209 holding `130825` while the live map had P10 on CC 0, `settings set
  209 130825` answered `ok` and changed nothing; writing `131071` and then
  `130825` put P10 back on CC 9/41. `assign_cc_map` therefore clears and then sets
  every key, and verifies against `modulation cc-map`, never against the keys.
- **A corrupted map mis-drives every take silently.** Renders came out as plain
  passthrough with P7 and P10 on CC 0 (driven by every P1 write) and P12 (Mix)
  unassigned; in other sessions P9 and P12 were on CC 0. `vmancer doctor` reports
  it as `midi.cc_map`; `vmancer cc-map --restore --yes` repairs it.
- **Slots learned a CC after link-dropped loads.** After a program load with the
  source HDMI link powered down for the load, one slot intermittently came back on
  a CC other than its own, with no button touched. Earlier releases sent an
  undefined controller (119 down to 102) after every load as a "sink" for an armed
  MIDI Assign; that is withdrawn, because CC 119 is a MIDI reset on the
  Videomancer, so the sink reset the device each time it was sent.
- **No software sink is needed.** Firmware `1.0.2-rc.4`'s release notes say "MIDI
  Assign only opens on a held P switch". `Videomancer` sends no extra CC: each load
  and recall rewrites all 12 keys and verifies, and `park` reads the map back,
  reassigns it if any slot differs, and records each changed slot's expected,
  before and after CCs in `cc_map_changes`, without attributing a cause. Callers
  driving a pass should `verify_cc_map()` at its end.

## Two clients on the port corrupt replies

The shell answers whoever writes, so two processes on one port interleave their
replies. One reply arrived as `{"ac@programs:{"author":"czartroniks",...`, a
`programs enumerate` reply spliced into another command's. `ShellClient` raises
`InterleavedReplyError` for such a line and sets aside a reply tagged for another
command group. A listing taken meanwhile can also be short or another author's,
which the enumerate `count` and `browse_author` cross-checks catch.

## The shell blocks on device-initiated work

On `1.0.1` the device re-indexes the program-picker author on its own, and the
shell answers nothing while it does: one `programs list` answered after 10.7 s,
with another author's list. A client with a 5 s reply deadline gave up, sent its
next command while the first was outstanding, and took the late `@programs:`
reply (same tag) as the answer to the next command; every later reply was then
shifted by one, and `programs authors` read a list reply as no authors. The guide
says to wait for each response before sending the next command; 0.6.5 drops reply
deadlines and resynchronises after any abandoned wait (protocol.md, "One command
at a time"). The guide notes `1.0.2` scans authors without blocking the port.

The program picker's author is restored at boot from settings key 19.

## A pipelined backlog can reboot the device

Writing about 20 KB of command lines while the shell was busy rebooted the unit:
boot count (key 9) rose and USB re-enumerated. A 1.3 KB backlog came back intact.
`ShellClient` only ever has one command outstanding, plus a short sync barrier
after an abandoned wait. `vmancer watch` flags a reboot from key 9 or a USB
re-enumeration.

## The settings store is smaller than the key space

`ram` reports `"settings":{"used":64,"capacity":64}`, while the documented keys
(19 named, 60 modulator, 12 MIDI Assign) number 91: keys 133-159 answer
`setting not found`. `vmancer watch` reads such a key as absent.

## Command set

`help` reports 64 commands. The guide documents them; earlier LZX docs did not
cover the `modulation` group, `program info`, the `video` and `remote` groups or
`settings get`/`set`/`reset`.

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

It hashes at about **79 kB/s**:

| File | Size | Time | Rate |
| --- | --- | --- | --- |
| `manifest.json` | 16526 B | 0.21 s | 77.0 kB/s |
| `blizzard.vmprog` | 322934 B | 3.98 s | 79.3 kB/s |
| `combing.vmprog` | 439221 B | 5.40 s | 79.4 kB/s |

Program binaries run 320-440 kB, so a hash takes seconds; like every command it
is awaited on liveness.

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
  holds the library. Root-directory puts never stalled. The tty transport now waits
  on a write that cannot progress for as long as the device stays on USB; the
  libusb transport keeps a 30 s bulk write bound, because pyusb reports no byte
  count for a timed-out write and it cannot be resumed.
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

The reference is zero on every slot, `pyvmancer.const.PARK_REFERENCE`. What a
slot does, and so whether zero on it darkens the output, is declared per program
in its `.vmprog` config, never assumed for a slot number.

## Vid Route Mode is settings key 1

SETTINGS > Vid Route Mode is settings key 1: `0` Analog In, `1` HDMI In, `2` Dual
In, `3` Standalone, `4` Auto, per the guide (`pyvmancer.const.ROUTE_MODES`). On
`1.0.1` it read 4 under Auto and 1 under HDMI In.

The route decides what the processor is fed, separately from the input `video
status` reports. With the route found on Dual In, `video status` still read
source `hdmi`, the HDMI input locked and the transmitter active, Passthru ran,
and every program output exact zero; setting HDMI In restored the picture. No
other `video status` field shows this, so a rig that needs one route compares
key 1 with it: `vmancer doctor --route hdmi-in` fails `video.route` on any other
value, and plain `vmancer doctor` reports the value unchecked.

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

## Liveness waits

Every command is awaited with no deadline, since the shell can block on work the
device starts itself (above), not only on `programs enumerate`, `program load`,
`fs hash` or `fs put`.

A wait ends only on positive evidence:

- the reply: `ok` (or the command's own payload) completes it, and an error
  reply is raised as `ShellError` with the device's message;
- the device leaving USB: the bus address the transport opened is read from
  sysfs on every quiet poll, and its absence raises `DeviceGoneError` at once;
- a transport error (`EIO` and the like) raises `TransportError`;
- for a load, `ProgramLoadError` once `fpga status` has moved off the state first
  seen after `program load` answered and then settles on a different program
  running (the load landed on, or reverted to, the wrong program). A different
  program running before anything has moved is the previous program still
  loaded, and is waited on.

While waiting, nothing else is sent. `ShellClient.report` receives a line every
`report_interval` seconds (`still loading czartroniks/Fray after 9.1s: shell busy
on 'programs enumerate czartroniks' 8.9s, usb up`); the interval is a display
cadence only. `ShellClient.stall` is an opt-in guard for unattended runs: when
set, the wait fails with `StallError` once nothing observable (the command
awaited, bytes or log lines received, the `fpga status` fields) has changed for
that many seconds. It is off by default; `vmancer load`, `shell` and `watch` take
`--stall`. An abandoned wait is followed by a sync barrier before the next
command (protocol.md). Where
sysfs cannot trace the transport to a Videomancer, presence reads as unknown and
the wait relies on transport errors.
