# Videomancer control protocol

Two independent control surfaces share one USB connection. The device enumerates
as a composite `16d0:14db` with six interfaces: CDC comm + CDC data (serial
shell), USB audio control + MIDI streaming (USB MIDI), mass storage (microSD),
and a vendor interface.

Sources: the [LZX Serial Port Guide](https://lzxindustries.net/instruments/videomancer/manual/serial-command-guide) (firmware `1.0.1`, `1.0.2` beta), which is the reference
for the serial shell, the LZX user manual and modulation guide, and the device
itself. What the guide leaves out or the device does differently is in
[firmware-notes.md](firmware-notes.md).

## Parameters

Twelve parameter slots, addressed 1-12 in the MIDI API and 0-11 in the serial
API.

| Slots | Control | Type |
| --- | --- | --- |
| P1-P6 | rotary knobs | continuous |
| P7-P11 | toggle switches | binary |
| P12 | fader | continuous |

Values are 10-bit, `0..1023`, midpoint `512`. A toggle reads as on at `>= 512`.
The device combines sources as `Parameter = Manual + Modulation + MIDI`, so a
MIDI CC is an offset on top of the panel position rather than a replacement for
it. `modulation set <slot 0-11> <manual> [time] [space] [slope]` over serial is
absolute; a CC never is. See [firmware-notes.md](firmware-notes.md) for what
that means for repeatable automation, and `Videomancer.park`.

P12 is the crossfader and gates the output even in programs whose `program info`
names it `Null 12`, so parking it at zero blacks the device out. The default
`pyvmancer.const.PARK_REFERENCE` therefore zeroes P1-P11 and leaves P12 open.

### Program parameters

Each loaded program names its own twelve parameters and declares a native range
for each; `program info` reports `{"name","id","parameters":[{"name","min","max"}]}`.
`pyvmancer.device.ProgramParameter` maps those native units linearly onto
`0..1023` and classifies the range into a `pyvmancer.const.ParamRole`:

| Declared range | Role | Sampling |
| --- | --- | --- |
| `0..1` | boolean | two states, on at combined `>= 512` |
| integer, no wider than the sweep resolution | quantised | one point per native position, `max-min+1` of them |
| anything else | continuous | the caller's sweep resolution |
| named `-` or `Null <n>` | unassigned | not sampled |

`Posterize 0..7` has 8 positions; sampling it at 32 steps wastes device time.
The enumerable/sweepable boundary is the caller's own resolution, so it is the
`sweep_steps` argument to `classify_param` and `Videomancer.parameters`.

## MIDI

Class-compliant USB MIDI, plus a rear TRS Type A jack. Both feed the same
pipeline. Default MIDI channel is Omni; `SYSTEM > Midi Channel` narrows it.

### Continuous controllers

| Slot | CC (MSB) | CC (LSB) |
| --- | --- | --- |
| P1-P6 | 0-5 | 32-37 |
| P7-P11 | 6-10 | 38-42 |
| P12 | 11 | 43 |

An MSB controller 0-31 pairs with an LSB at MSB + 32, giving optional 14-bit
resolution; `modulation cc-map` reports these as `auto` pairings, and an MSB of 32
or above with `lsb: 255` (7-bit). Assignments are made on the device by
long-pressing a modulator button (MIDI Assign: the next CC received is learned).
There is no remote-assign MIDI message, but settings keys 200-211 carry the map
over serial; see [firmware-notes.md](firmware-notes.md#cc-map). pyvmancer writes
the controller's map to them on every load.

### Notes

Notes 0-11 map to slots P1-P12. Note-on fires the slot's operator where that
operator is note-triggered (Bouncing Ball, Pendulum, MIDI Turing, and the
envelope operators); note-off or velocity 0 releases it.

### Program change and clock

Program Change recalls a preset. MIDI Clock at 24 PPQN drives tempo-locked
operators, with Start/Continue/Stop controlling transport playback; Stop resets
timecode to `00:00:00:00`. MIDI Timecode overrides the internal timecode.

## Modulation operators

Twenty-five documented operators. `modulation source <index> <id>` assigns one to
a slot; `modulation set <index> <manual> [time] [space] [slope]` sets the manual
value and the three shared mod knobs. The time/space/slope knobs only take effect
while the slot has a non-disabled source.

| ID | Operator | Category | Per-line | Clocking |
| --- | --- | --- | --- | --- |
| 0 | Disabled | - | no | - |
| 1 | Free LFO | oscillator | no | free |
| 2 | Random | random | no | free |
| 3 | Turing Machine | random | no | free |
| 4 | Bouncing Ball | physics | no | free |
| 5 | Logistic Map | random | no | free |
| 6 | Euclidean Rhythm | sequencing | no | free |
| 7 | Sync LFO | oscillator | no | tempo |
| 8 | Audio Input | external | yes | free |
| 9 | Comparator | external | yes | free |
| 10 | Pendulum | physics | no | free |
| 11 | Drift | random | no | free |
| 12 | Ring Mod | external | yes | free |
| 13 | Cellular | random | no | free |
| 14 | Pulse Width | oscillator | no | free |
| 15 | Peak Hold | external | yes | free |
| 16 | Field Accum | external | no | free |
| 17 | Slew Limiter | external | no | free |
| 18 | Perlin Noise | random | no | free |
| 19 | Wavefolder | oscillator | no | free |
| 20 | Clock Div | sequencing | no | tempo |
| 21 | Prob Gate | sequencing | no | free |
| 22 | Quantizer | external | yes | free |
| 29 | MIDI Turing | random | no | free |
| 30 | CV Input | external | yes | free |

IDs 23-28 are not documented and are deliberately absent from
`pyvmancer.const.Operator`; read `program state` to discover what a slot is
actually using rather than guessing.

Per-line operators render a distinct value per scanline on slots P1-P6 and P12;
toggle slots stay at field rate. Tempo-clocked operators output only while the
transport is playing.

## Serial shell

See the [LZX Serial Port Guide](https://lzxindustries.net/instruments/videomancer/manual/serial-command-guide) for the wire protocol and command reference. In short:
newline-terminated ASCII over USB CDC, the baud rate ignored, 511 bytes per
command line, replies of at most 1536 bytes (`programs list` and `fs ls` page).

- `@<topic>:<payload>` on success, the topic naming the command family
- `!<code>:<message>` on failure
- unprefixed lines are log or MIDI monitor output
- `@program:invalidated:variant_mismatch` arrives unasked; `ShellClient` keeps it
  in `unsolicited` and never takes it for a reply

Most commands answer one line; `cpu` answers one `@cpu:` line per core, which
`ShellClient.replies` collects and `cpu()` merges.

### Error codes

A code is `domain << 26 | item`: domain 22 is the shell, 23 presets.
`pyvmancer.const.ShellErrorCode` and `PresetErrorCode` hold the documented codes
in full (`ShellErrorCode.PARSE_ERROR == 1476395010`), so a presets item is never
mistaken for the shell item with the same number; `decode_error_code` and
`ShellError.domain`/`.item` split one.

### One command at a time

The shell is single-threaded and serves one connection. `SerialTransport` opens
the tty exclusively (`flock` and `TIOCEXCL`), raising `PortBusyError` when another
process holds it, and drains stale input on open. `ShellClient` never sends a
command while another is outstanding: every reply is awaited on liveness (see
firmware-notes, "Liveness waits"), never a deadline. A wait that is abandoned (the
opt-in stall guard, `KeyboardInterrupt`, a transport error, a spliced line) leaves
the client unsynchronised, as does opening it. The next command first sends a sync
barrier, `zz<nonce>`, an unknown command whose `unknown_command` error echoes the
line; the shell answers in order, so anything still owed to an abandoned command
arrives before that echo and is discarded (`ShellClient.discarded`).

Replies are also checked for shape: `programs authors`, `programs enumerate`,
`programs list` pages, `fpga status`, `settings get` and each `cpu` line must be
JSON objects carrying their documented fields, or `ReplyShapeError` is raised
rather than a default read.

### Settings keys

`pyvmancer.const.SettingKey`, `SETTING_NAMES` and `SETTING_VALUES` carry the
guide's Setting Keys table (1 Video Route Mode, 8 Preferred Timing, 9 boot count,
19 program author, ...), `TIMING_CODES` the Preferred Timing codes (the FPGA's
4-bit timing value, not a list position), `modulator_key(channel, field)` the
100-159 block (`100 + channel * 5 + field`) and `CC_MAP_SETTING_KEY` the 200-211
MIDI Assign block, packed as bits 0-7 CC, 8-15 LSB CC (255 none) and 16
auto-pair. `setting_name` and `describe_setting` render a key and value with
those names; `vmancer watch` uses them. Writing key 7 loads a program.

### Command set

`help` returns the authoritative list for the connected firmware. On
`1.0.0-rc.37` it is 64 commands:

| Group | Commands |
| --- | --- |
| general | `version`, `serial`, `status`, `help`, `language`, `reboot bootloader` |
| programs | `programs list`, `programs authors`, `programs enumerate`, `program load`, `program info`, `program state`, `program stream` |
| presets | `program presets list/get/apply/save/delete/rename` |
| modulation | `modulation status/source/set/reset/cc-map/latch`, `modulation audio status` |
| video | `video status`, `video input`, `video timing` |
| transport | `transport status/bpm/play/stop` |
| fpga | `fpga status`, `fpga register`, `fpga preferred-variant` |
| remote | `remote enable/disable/status/write` |
| settings | `settings export/import/get/set/reset` |
| midi | `midi monitor on/off/verbose` |
| storage | `msd enter/exit/status`, `fs info/caps/ls/stat/mkdir/rm/rename/read/write/put/hash` |
| diagnostics | `cpu`, `ram`, `log level`, `screen navigate` |

### Signatures confirmed on hardware

```
modulation set <index 0-11> <manual> [time] [space] [slope]
modulation source <index 0-11> <source>
settings get <key_id>
program stream <size> [name]
video timing <NTSC|PAL|480p|576p|720p60|720p50|1080i60|1080p30|...>
fpga register <addr> <value>
remote <enable|disable|status|write>
language [en_US|de_DE|fr_FR|es_ES|...]
```

### Video

`video status` reports the output timing, the selected input and a set of lock
flags; `pyvmancer.video.VideoStatus` parses it. The top-level `locked` flag
tracks genlock, so it reads false whenever timing is overridden even though the
input is fine, and the selected input's own sub-status (`hdmi`/`analog`) is the
authoritative field in both directions. Every one of these flags is advisory —
see [firmware-notes.md](firmware-notes.md).

`video timing <standard>` forces an output standard and its usage string
enumerates what the firmware accepts, which is how `ShellClient.video_timings`
discovers the set rather than assuming one. Bouncing the timing away and back is
the strongest output reset the shell offers (`ShellClient.resync`); it drops the
input selection and leaves `overridden: true`.

### Filesystem

All paths start with `sd:/` and may not contain `..`. `fs read`/`fs write` carry
base64 payloads and `fs put` exists for bulk upload with an 8 MiB cap.

`fs read <path> <offset> <count>` answers with a JSON envelope:

```json
{"data":"ewogICJmb3JtYXRfdmVyc2lvbiI6ICIxLjAiLAog...","read":96}
```

`fs caps` reports the transfer limits, and `read_max_bytes` (256 on
`1.0.0-rc.37`/`rc.40`) bounds that **base64 payload**, not the decoded bytes: a
request for 256 decoded bytes encodes to 344 characters and is truncated. A
request must therefore ask for at most `read_max_bytes // 4 * 3` bytes, which is
what `pyvmancer.shell.decoded_limit` computes.

`fs caps` also advertises `fs_hash: 1`, for `fs hash <path>` →
`{"hash": "<sha256 hex>", "size": n}`, computed on the device at roughly 79 kB/s
— slow enough that `ShellClient.hash_file`, like every command, awaits it on
liveness. See [firmware-notes.md](firmware-notes.md).

`fs write` carries base64 too, and the whole command line is capped at 511
bytes, so a chunk must leave room for the path and offset beside its payload.

`sd:/programs/manifest.json` lists the SD-installed program library:

```json
{"format_version":"1.0","version":"1.0.2","created":"...","product":"...",
 "programs":[{"name":"combing","file":"lzx/combing.vmprog",
   "program_id":"com.lzxindustries.combing","program_name":"Combing",
   "program_version":"1.0.0","categories":["Signal"],"program_type":"processing",
   "description":"Interlace comb artifact simulation ...","author":"Lars Larsen"}]}
```

`pyvmancer.programs.ProgramManifest` parses it. Coverage is partial: firmware
built-in programs are not listed, so `programs list` is normally longer and
those entries have no description available from any source.

### Program authors

The device indexes one author at a time, the one selected in the program picker.
`programs authors` returns `{"count","authors":[...]}`; `programs enumerate
<author>` switches the active author and rebuilds the list, replying
`{"author","skipped","count","before","overflow","active_author"}` after scanning
every program header in the folder. `programs list` acts on the active author;
from `1.0.1` `program load` searches every author and switches the picker. `ShellClient.programs()` visits every author (`lzx`
first, then the rest sorted) and qualifies non-`lzx` names as `author/Name`, since
the same name can exist under two authors; `load_program` accepts the same form.
Firmware without `programs authors` is listed and loaded as a single set; only
an `unknown_command` or `parse_error` means that, and any other
failure of `programs authors` raises rather than listing the active author alone.

Every listing enumerates its author and checks the names against the reply:
`author` must be the one asked for, the paged names must number `count` and be
unique, none may contain `/`, and `fpga status` `browse_author`, when reported,
must still be that author once listed. A listing that fails is re-enumerated and
re-listed once, then raises `ProgramListError` naming both attempts. A load skips
`programs enumerate` only when `browse_author` already names its author, since
another client can switch it.

### Reply tags

A reply is tagged with its command's first word (`@programs:` for `programs
list`, `@fpga:` for `fpga status`). `ShellClient` sets aside a reply tagged with
another command group's word as another client's, in `foreign_lines`, and raises
`InterleavedReplyError` for a line with a reply or error spliced into it, e.g.
`@modulation:{"ac@programs:{"author":...`, which two processes on the port produce.
