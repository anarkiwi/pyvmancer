# Videomancer control protocol

Two independent control surfaces share one USB connection. The device enumerates
as a composite `16d0:14db` with six interfaces: CDC comm + CDC data (serial
shell), USB audio control + MIDI streaming (USB MIDI), mass storage (microSD),
and a vendor interface.

Sources: the LZX user manual and modulation guide, plus the `help` output of a
unit running firmware `1.0.0-rc.37`. Where the two disagree, the device wins and
the difference is noted in [firmware-notes.md](firmware-notes.md).

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
it.

Each loaded program names its own twelve parameters and declares a native range
for each; `program info` reports them. `pyvmancer.device.ProgramParameter` maps
those native units linearly onto `0..1023`.

## MIDI

Class-compliant USB MIDI, plus a rear TRS Type A jack. Both feed the same
pipeline. Default MIDI channel is Omni; `SYSTEM > Midi Channel` narrows it.

### Continuous controllers

| Slot | CC (MSB) | CC (LSB) |
| --- | --- | --- |
| P1-P6 | 0-5 | 32-37 |
| P7-P11 | 6-10 | 38-42 |
| P12 | 11 | 43 |

The LSB controller is always MSB + 32, giving optional 14-bit resolution.
Confirmed against `modulation cc-map`, which reports these as `auto` pairings.
Assignments are made on the device by long-pressing a modulator button; there is
no remote-assign MIDI message, so `MidiController.assign_cc` only mirrors an
assignment already made on the hardware.

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

Newline-terminated ASCII over USB CDC; the baud rate is ignored. Maximum 511
bytes per command line.

- `@<command>:<payload>` on success
- `!<code>:<message>` on failure
- unprefixed lines are log output

Firmware `1.0.0-rc.37` tags error codes as `0x58000000 | n`, so `1476395010` is
code 2. `pyvmancer.shell.decode_error_code` strips the tag.

The shell is single-threaded and expects one connected terminal at a time.

### Command set

`help` returns the authoritative list for the connected firmware. On
`1.0.0-rc.37` it is 64 commands:

| Group | Commands |
| --- | --- |
| general | `version`, `serial`, `status`, `help`, `language`, `reboot bootloader` |
| programs | `programs list`, `program load`, `program info`, `program state`, `program stream` |
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

### Filesystem

All paths start with `sd:/` and may not contain `..`. `fs caps` reports transfer
limits; on `1.0.0-rc.37` `read_max_bytes` is 256, so `fs read` must be chunked at
or below that. `fs read`/`fs write` carry base64 payloads. `fs put` exists for
bulk upload with an 8 MiB cap.
