# Firmware notes

Observations from a Videomancer running `1.0.0-rc.37`, serial `E464B0605F113625`.
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

## `fs read` is capped at 256 bytes

`fs caps` reports `read_max_bytes: 256`. Larger requests fail, so reads must be
chunked at or below that. `pyvmancer` queries `fs caps` and uses the reported
limit.

## Modulation knobs need an active source

`modulation set <index> <manual> <time> <space> <slope>` accepts and acknowledges
all five arguments even when the slot's source is `Disabled`, but `t`/`sp`/`sl`
in `program state` stay at 512. Assign a source first, then set the knobs.

## Manual value and MIDI are separate

A MIDI CC does not change `m` in `program state`. It contributes to the combined
`o` output in `modulation status` while leaving the stored manual value alone,
matching the documented `Manual + Modulation + MIDI` sum. To change the stored
value, use `modulation set` over serial.

## Program inventory

21 programs on this unit, several of which are not in the published list
(`Colorbars`, `Passthru`), and some published names are absent (`Fauxtress`,
`Moire`, `Mycelium`, `Perlin`). `pyvmancer.const.EMBEDDED_PROGRAMS` is a
convenience list only; call `programs list`.

## MIDI byte counts

`/proc/asound/card*/midi0` Tx counts are lower than the bytes handed to the
library because the ALSA sequencer applies running-status compression. A
high-resolution parameter write plus a program change plus a note pair measured
21 bytes rather than 26.
