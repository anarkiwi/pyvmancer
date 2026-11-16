# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
[semantic](https://semver.org/), with the 0.x caveat that the public API may
still move between minor releases.

## [0.6.4] - 2026-11-16

Patch: the Vid Route Mode check, and program listings that are checked rather
than trusted.

### Added

- `pyvmancer.route`: `read_route(shell)` reads Vid Route Mode from settings key 1;
  `route_name`, `describe_route` and `parse_route` know the observed values only,
  1 = HDMI In and 4 = Auto (`ROUTE_MODES`, `ROUTE_HDMI_IN`, `ROUTE_SETTING_KEY`).
- `diagnose(shell, midi, route=...)` and `diagnose_route`: the `video.route` finding,
  reported unchecked without an expected value. `vmancer doctor --route VALUE`.
- `shell.read_setting(shell, key)`, `shell.listing_problems(...)`.
- `ShellClient.active_author()` reads `fpga status` `browse_author`;
  `enumerate_author(..., force=True)`.
- `InterleavedReplyError`, `ProgramListError`; `ShellClient.foreign_lines`.

### Fixed

- `programs()` read any failure of `programs authors` as firmware without authors
  and listed the active author alone, unqualified. Only an unknown-command or usage
  error means that now; anything else raises.
- Each author's listing is checked against its `programs enumerate` reply (author,
  `count`, unique names, no `/`) and the device's active author, re-enumerated and
  re-listed once, then raises naming both attempts.
- The active-author skip trusted this client's memory; it now trusts the device's
  `browse_author`, which another client can change.
- A reply tagged for another command group is set aside, and a line with another
  reply spliced into it raises, instead of either being parsed as this command's.

## [0.6.3] - 2026-11-16

Patch: device-side work is awaited on liveness, not a deadline. The shell answers
nothing while it enumerates an author or reads a program from the card, for
longer than any fixed reply deadline is safe to assume, so a wait now ends only
on the reply, an error reply, a transport error, or the device leaving USB.

### Added

- `pyvmancer.liveness.Watch`: progress reports at a display cadence, USB presence
  on every quiet poll, and an opt-in stall guard.
- `ShellClient.report`, `report_interval` and `stall` (off by default);
  `ShellClient.watch(what)`; `command(..., watch=...)` awaits a reply with no
  deadline.
- `ByteTransport.present()`; the tty and libusb transports read it from sysfs by
  the bus address they opened. `discovery.attached(bus, address)`.
- `DeviceGoneError` (a `TransportError`) and `StallError`.
- `ShellClient.export_settings()`; `shell.fpga_key(status)`.
- `vmancer load --stall SECONDS`; the CLI prints progress lines on stderr.

### Changed

- `load_program` and `wait_loaded` wait on liveness. `wait_loaded(program=None,
  watch=None, sleep=None)` drops `timeout` and `clock`; `ProgramLoadError` is
  raised only when `fpga status`, having moved off its first state, settles on a
  different program running. It takes an optional `reason`.
- `programs enumerate`, `program presets apply`, `fs hash`, `fs put` and
  `settings export`/`import`/`reset` wait on liveness. `hash_file` and `put_file`
  drop `timeout=`.
- The tty transport waits on a write that cannot progress for as long as the
  device stays on USB; its 30 s write deadline and `write_timeout` argument are gone.

### Removed

- `ENUMERATE_TIMEOUT`, `HASH_BYTES_PER_SECOND`, `HASH_TIMEOUT_FACTOR`,
  `PUT_BYTES_PER_SECOND`, `PUT_TIMEOUT_FACTOR`, `ShellClient.hash_timeout`,
  `ShellClient.put_timeout`, `serial_tty.WRITE_TIMEOUT`.

## [0.6.2] - 2026-11-16

Patch: a load waits until the device runs the program, because `program load`
can acknowledge before the FPGA reconfiguration completes and the `program info`
read that follows then fails with `no program loaded`.

### Added

- `ShellClient.wait_loaded(program=None, timeout=None)`: polls `fpga status` until
  `program` (any when None) is `running` and `configured` and `program info`
  answers, bounded by the shell's reply deadline; returns the last status.
- `ProgramLoadError` (`program`, `status`), raised when a load never completes and
  naming the last `fpga status`.
- `shell.fpga_running(status, program=None)`.

### Fixed

- `ShellClient.load_program` (and so `Videomancer.load_program`) waits for the load
  before returning, so the parameter read and CC-map assignment no longer race it.
- `Videomancer.apply_preset` and `select_preset` wait for the device the same way
  before assigning the CC map.

## [0.6.1] - 2026-11-16

Patch: the device's MIDI CC map is checked and assigned, because a learned
assignment on the unit silently mis-drives every CC write.

### Added

- `pyvmancer.ccmap`: `cc_map_report(shell, midi=None)` returning a `CcMapReport`
  (`expected`, `actual`, `mismatched`, `shared`, `unassigned`, `armed`, `ok`);
  `assign_cc_map(shell, midi=None, apply=True)` returning a `CcMapAssignment`,
  which writes every slot of the controller's map through settings keys 200-211
  (cleared, then set) and raises `CcMapError` unless `modulation cc-map` then
  matches, refusing to write when any key is outside the observed encoding;
  `verify_cc_map` (read-only, raises on any difference); `reassert_cc_map`
  (repairs only a capture of the sink CC); `expected_cc_map`; `sink_cc`.
- `ASSIGN_SINK_CCS`: undefined controllers 119..102. `Videomancer` sends the first
  one the map leaves free after each load and preset recall, and first in `park`,
  so a MIDI Assign armed without a button press learns it rather than a slot's CC.
- `Videomancer.load_program`, `apply_preset`, `select_preset` re-read the program
  and assign the CC map; `park` reasserts it afterwards. `Videomancer.cc_assignment`
  keeps the last assignment and `assign_events` every armed MIDI Assign detected
  (`time`, `program`, `phase`, `params`); each assignment logs one line on
  `pyvmancer.device`, e.g. `cc-map: assigned P1..P12 -> CC0..11 (was: ok)`.
- `diagnose(shell, midi=None)` adds `midi.cc_map`; `diagnose_cc_map(report)`.
- `latency.probe_cc_map(device)` returning a `CcProbeReport`: writes each slot's CC
  alone over MIDI and records which slots read back moved, restoring manual values
  and CC offsets; `diagnose_cc_probe(report)` gives `midi.cc_probe`.
- `MidiController.control_change(cc, value=0)` and `midi.lsb_partner(cc)`.
- `vmancer cc-map [--restore [--yes] | --probe]`; `vmancer load` goes through
  `Videomancer.load_program`, so it assigns the map too.

### Changed

- 14-bit pairing is MSB 0..31 only, as the device reports; MSB 32..95 now sends
  7-bit instead of an LSB the device does not listen on.

## [0.6.0] - 2026-11-15

Minor bump: a MIDI CC write to readback latency diagnostic, so whether the
device keeps up with a CC rate is a command rather than a script. Additive only.

### Added

- `pyvmancer.latency.measure_latency(device, *, params, rates, seconds, samples,
  timeout, clock, sleep, report)` returning a `LatencyReport`: readback round
  trip, single-write latency samples, and per-rate `RateResult` sentinel lag
  after a paced ramp. Targets are `manual + offset`; parameters with a modulation
  source, toggles and manual values without CC headroom are refused, and every
  touched parameter is restored to its prior combined value.
- `diagnose_latency(report)` and `diagnose_midi(device)`: `midi.latency` (median
  single write over two frames plus the readback round trip) and `midi.backlog`
  (a sentinel lagging a single write by over a frame, naming the fastest rate
  that kept up). The frame period comes from the output timing.
- `video.frame_period(timing)` and `ShellClient.manual_values()`.
- `vmancer latency [--param N[,N...]] [--rates R,...] [--seconds S]` and
  `vmancer doctor --latency`; `diagnose(shell)` stays read-only.

## [0.5.0] - 2026-11-15

Minor bump: device diagnostics, so troubleshooting is a command rather than a
script. Additive only.

### Added

- `pyvmancer.diagnose`: `Finding(check, ok, detail, fix)`, `diagnose_video(status)`,
  `diagnose(shell)` and `report(findings)`. Checks `firmware.version`,
  `fpga.running`, `video.input_locked`, `video.genlock` (output held by the
  front-panel route), `video.output_active` (HDMI transmitter idle) and
  `video.output_connected`; checks whose inputs the firmware omits are skipped.
- `ShellClient.apply_video_timing(timing)`: sets, settles and reads back the
  output timing, raising `VideoTimingIgnored` when an `ok` changed nothing.
- `VideoStatus.output_timing` and `VideoStatus.source_timing`.
- `vmancer doctor`, `vmancer video status|timings|timing|input`, and
  `vmancer shell --timeout`.

## [0.4.0] - 2026-10-26

Minor bump: program listing and loading become author-aware. `programs()` now
returns programs from every author, with non-`lzx` names qualified as
`author/Name`, where it previously saw only the active author.

### Added

- `ShellClient.authors()` and `ShellClient.enumerate_author(author)`, wrapping
  `programs authors` and `programs enumerate`. Enumerating uses its own
  `ENUMERATE_TIMEOUT` and is skipped when the author is already active.
- `Videomancer.authors()`, `vmancer authors`, and `vmancer programs --author`.

### Changed

- `ShellClient.programs(author=None)` / `Videomancer.programs` list every author
  (`lzx` first, then sorted; non-`lzx` names are `author/Name`), or one author's
  plain names. Firmware without `programs authors` keeps the single paged list.
- `load_program` accepts `author/Name`, enumerating that author first; a bare
  name means `lzx`.

## [0.3.0] - 2026-08-08

Minor bump: two new subsystems and three fixes. The API is additive, but two of
the fixes change behaviour a caller may have been relying on — `write_file`
could not succeed at all on `1.0.0-rc.46` before this, and the transports now
allow a blocked write thirty seconds rather than two.

### Added

- `pyvmancer.firmware`: firmware release discovery and the UF2 bootloader
  upgrade path, plus a `vmancer firmware` command group (`list`, `status`,
  `download`, `upgrade`). Releases come from `lzxindustries/videomancer-firmware`,
  which carries several LZX products under `<product>/<version>` tags, so
  Videomancer builds are selected by tag prefix rather than by GitHub's "latest
  release" endpoint — that endpoint ignores prereleases, and every Videomancer
  release so far is one, so it would either answer with another product's build
  or nothing at all. Ordering is semver precedence, not string order, which is
  what keeps `1.0.0-rc.46` ahead of `1.0.0-rc.9`.
- `inspect_uf2` validates a UF2 whole before it can be flashed: block magic,
  sequential numbering, the declared block count against the file, payload
  bounds, and the target MCU family (the device's images are RP2040,
  `0xe48bff56`, based at `0x10000000`). A download that fails validation is
  deleted rather than left on disk looking flashable.
- `vmancer firmware upgrade` is a dry run unless `--yes` is passed, and refuses
  to reflash the running version without `--force`.

- `upgrade(volume=...)` / `--volume` accepts an already-mounted bootloader, for
  sessions where udisks cannot obtain the authorization to mount one.
- `pyvmancer.library`: program library releases and installing them onto the SD
  card, plus a `vmancer library` command group (`list`, `status`, `download`,
  `install`). Archives are verified against the release's published
  `checksums.sha256` before anything is uploaded. Install is a dry run unless
  `--yes`, skips files already on the card at the archive's size, and resumes
  after a dropped link. It is a separate group from the existing `vmancer
  programs` listing command, which keeps working as it did.
- `ShellClient.put_file`, wrapping `fs put`: the device takes the payload as raw
  bytes after a size handshake, at 157-166 kB/s against roughly 3 kB/s for the
  base64 `fs write` path. A 14 MB library takes about 90 seconds rather than
  days.
- `ShellClient.listdir` pages `fs ls` and de-duplicates it.

### Fixed

- `ShellClient.write_file` raised `VmancerError` for every write on
  `1.0.0-rc.46`: `fs write` answers `{"written": n}` there where `rc.37` and
  `rc.40` answered `ok`. Both forms are now accepted, and a count short of what
  was sent is an error rather than a silent truncation.
- The serial and libusb transports gave a blocked write 2 seconds before failing.
  The device stalls longer than that while committing an `fs put` payload to the
  card, and failing there strands it consuming the link as file content --
  unrecoverable short of a power cycle. Both now allow 30 seconds.
- `ShellClient._await_reply` drained every buffered line but returned on the
  first reply, discarding anything that arrived behind it in the same read.

### Notes

Verified end to end on hardware, `1.0.0-rc.40` to `1.0.0-rc.46`.

- Flashing tolerates the bootloader rebooting on the last block: once every byte
  has been handed over, the volume disappearing under the write, flush or close
  is the expected ending rather than a failure.
- Bootloader volumes are recognised by the UF2 spec's `INFO_UF2.TXT` marker, not
  by a volume label, and mounted with `udisksctl` when nothing automounted them.
- The bootloader's FAT volume is a *partition* (`/dev/sdc1`), not the disk node
  the mass-storage interface reports (`/dev/sdc`), which udisks rejects as "not a
  mountable filesystem". `volume_nodes` resolves it from sysfs.
- Copying is not flashing: writes to a page-cached vfat mount return long before
  the data reaches the device, so the transfer actually completes at `fsync` and
  takes minutes for a 24 MB image. The version read back afterwards is what
  proves the flash, not the write returning.

The library install was verified end to end against `1.0.0-rc.46`: all 38 files,
14026467 bytes, in 91 seconds, with two of them read back and confirmed
byte-identical to the published archive.

- `fs hash` returns **wrong** digests on `1.0.0-rc.46`, verified against the
  published archive, so `hash_file` is not trustworthy on that firmware and the
  installer compares sizes instead. See `docs/firmware-notes.md`.
- The device builds its program index at boot, so newly installed programs are
  neither listed nor loadable until it is power cycled. `install` reports
  `restart_required` and says so.

## [0.2.1] - 2026-08-01

Patch: a bug fix and a defensive bound. No API removed; `hash_file` gained an
optional argument.

### Fixed

- `ShellClient.hash_file` inherited the 5.0s default command timeout, but the
  device hashes at about 79 kB/s, so every program binary large enough to matter
  raised `ShellTimeoutError`. Measured on hardware: 16526 B in 0.21s, 322934 B
  in 3.98s, 439221 B in 5.40s — and binaries run 320-440 kB, so the largest
  failed every time while the small manifest always worked, which is how it
  shipped. `hash_file` now derives a deadline from the size `stat` reports
  (`HASH_BYTES_PER_SECOND`, `HASH_TIMEOUT_FACTOR`, floored at the client's own
  timeout) and takes an explicit `timeout=` for callers who know better.
- `ShellClient.write_file` sized its chunks without accounting for the path and
  offset in the command line, so a long enough path could push the rendered
  command past the shell's 511-byte limit and raise `ValueError`. The chunk is
  now bounded by the space the line actually leaves.

### Audited, unchanged

`read_file` issues many small commands, each carrying at most
`decoded_limit(read_max_bytes)` bytes, so no single one approaches the default
timeout; the same holds per chunk for `write_file`. `fs put` is not wrapped by
this library, and `settings export` remains unwrapped per the firmware notes.

## [0.2.0] - 2026-08-01

Minor bump: two fixes plus new, additive API. Nothing was removed or changed in
meaning, so no 0.x consumer has to adapt, but `ShellClient.read_file` now
returns different (correct) bytes and `_read_limit` a different number.

### Fixed

- `ShellClient.read_file` base64-decoded the whole `fs read` reply. The firmware
  answers with a JSON envelope, `{"data":"<base64>","read":n}`, so every chunk
  decoded to garbage and no file could be read off the device correctly. The
  payload is now taken from `reply.json()["data"]`.
- `fs caps` `read_max_bytes` bounds the **base64 payload**, not the decoded
  bytes. Requesting 256 decoded bytes encodes to 344 characters, overruns the
  limit and truncates the reply. Requests are now sized `read_max_bytes // 4 * 3`
  (192 decoded bytes on firmware `1.0.0-rc.37`/`rc.40`), exposed as
  `shell.decoded_limit`.

### Added

- `ShellClient.hash_file` wrapping `fs hash`, advertised as `fs_hash` in
  `fs caps`; the digest is a stable cache key for anything derived from a
  program binary.
- `pyvmancer.programs` with `ProgramManifest`/`ProgramEntry`, plus
  `ShellClient.program_manifest` and `Videomancer.program_manifest`, parsing
  `sd:/programs/manifest.json`. Manifest coverage is partial: firmware built-ins
  are not listed, and `ProgramManifest.missing` reports which reported programs
  it does not describe.
- Typed program parameters. `ParamRole` and `const.classify_param` interpret a
  declared range as boolean (`0..1`, on at `BOOL_THRESHOLD`), quantised (an
  integer range no wider than the sweep resolution), unassigned (`-`, `Null <n>`)
  or continuous. `ProgramParameter` carries `role`, `steps`, `assigned`,
  `crossfader`, `as_bool` and `sample_values`.
- `ShellClient.resync`, recovering an output that has stopped passing frames by
  bouncing `video timing` and restoring it. The alternate standard is discovered
  from the firmware's own `video timing` usage string, or supplied by the caller.
  The bounce drops the input selection, so it is reasserted, and it leaves
  `overridden: true`. Verification is by polling `video status` only.
- `pyvmancer.video.VideoStatus` and `ShellClient.video_state`, encoding the lock
  semantics: the top-level `locked` flag tracks genlock, the selected input's
  sub-status is authoritative, and every one of these flags is advisory.
- `ShellClient.combined_values`, the `Manual + Modulation + MIDI` sum for all 12
  slots, and `Videomancer.park`, which drives every manual value to a reference
  so a CC becomes an absolute address. Writes are paced (an unpaced burst drops
  the LSB of 14-bit CC pairs) and the readback settles before verification. The
  default `PARK_REFERENCE` leaves P12 open, because P12 is the crossfader and
  gates the output even in programs that name it `Null 12`.
- `vmancer manifest`, `vmancer hash <path>` and `vmancer resync` CLI verbs.

## [0.1.0] - 2026-07-30

Initial release: MIDI and serial control paths, device discovery, transports and
the `vmancer` CLI.
