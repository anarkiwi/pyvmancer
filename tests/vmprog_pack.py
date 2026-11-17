"""Pack minimal ``.vmprog`` packages the way the Videomancer SDK's TOML converter and packer write them."""

import hashlib
import struct
import zlib

CONTROL_MODES = {"linear": 0, "boolean": 4, "steps_4": 5, "steps_256": 11, "polar_degs_90": 12}
SLOTS = {f"rotary_potentiometer_{i}": i for i in range(1, 7)}
SLOTS |= {f"toggle_switch_{i}": i for i in range(7, 12)}
SLOTS["linear_potentiometer_12"] = 12


def _param(param):
    """One 572-byte parameter config; value labels set the mode and a 0..N-1 range, as the SDK does."""
    labels = param.get("value_labels", [])
    if labels:
        mode = CONTROL_MODES["boolean"] if len(labels) == 2 else CONTROL_MODES["linear"]
        low, high = 0, len(labels) - 1
        init = labels.index(param.get("initial_value_label", labels[0]))
        display = (low, high, 0)
    else:
        mode = CONTROL_MODES[param.get("control_mode", "linear")]
        low, high, init = (
            param.get("min_value", 0),
            param.get("max_value", 1023),
            param.get("initial_value", 512),
        )
        display = (
            param.get("display_min_value", low),
            param.get("display_max_value", high),
            param.get("display_float_digits", 0),
        )
    packed = struct.pack(
        "<IIHHHhhBB2x", SLOTS[param["parameter_id"]], mode, low, high, init, *display, len(labels)
    )
    packed += param["name_label"].encode().ljust(32, b"\0")
    packed += b"".join(label.encode().ljust(32, b"\0") for label in labels).ljust(16 * 32, b"\0")
    return packed + param.get("suffix_label", "").encode().ljust(4, b"\0") + b"\0\0"


def pack_config(program, parameters, presets=()):
    """The 7936-byte program config for a ``[program]`` table, ``[[parameter]]`` and ``[[preset]]`` lists."""
    config = bytearray(7936)
    struct.pack_into("<64s3H", config, 0, program["program_id"].encode(), *program.get("version", (1, 0, 0)))
    struct.pack_into("<I32s64s", config, 82, program.get("core_id", 1), program["program_name"].encode(), b"")
    struct.pack_into("<64s", config, 118, program.get("author", "").encode())
    struct.pack_into("<HBB", config, 726, len(parameters), len(presets), 1)
    for index, param in enumerate(parameters):
        config[730 + 572 * index : 730 + 572 * (index + 1)] = _param(param)
    for index, preset in enumerate(presets):
        values = [preset.get(slot, 0) for slot in SLOTS]
        struct.pack_into("<16s12H", config, 7594 + 40 * index, preset["name"].encode(), *values)
    struct.pack_into("<HBH", config, 7914, program.get("supported_timings", 0), 0, program.get("delay", 0))
    return bytes(config)


def pack_vmprog(config, compress=False, digest=None, entry_type=1):
    """A package of one config TOC entry; ``digest`` overrides the BLAKE2b-256 of the stored bytes."""
    stored = config
    if compress:
        packer = zlib.compressobj(9, wbits=-10)
        stored = packer.compress(config) + packer.flush()
    digest = digest or hashlib.blake2b(stored, digest_size=32).digest()
    toc = struct.pack(
        "<IIII32sI12x", entry_type, int(compress), 128, len(stored), digest, len(config) * compress
    )
    header = struct.pack("<4sHHHHIIIII32x", b"VMPG", 1, int(compress), 64, 0, 128 + len(stored), 0, 64, 64, 1)
    return header + toc + stored
