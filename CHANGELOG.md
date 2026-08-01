# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions are
[semantic](https://semver.org/), with the 0.x caveat that the public API may
still move between minor releases.

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
