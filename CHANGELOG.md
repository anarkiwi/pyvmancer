# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
[semantic](https://semver.org/), with the 0.x caveat that the public API may
still move between minor releases.

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
