"""Build single-hook isolation images to find which one bricks.

Eight full automation builds have failed on hardware while every build without
them boots, and static review plus real-code emulation clears every component.
So the fault is in an interaction that inspection cannot see; the only way left
is to enable one hook at a time.

    python analysis\\build_circuit_automation_isolate.py record
    python analysis\\build_circuit_automation_isolate.py playback

Both sit on the same LFO base as the failing full build, so a boot/no-boot
result attributes the fault to that hook alone.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parent
ROOT = ANALYSIS_DIR.parent
sys.path.insert(0, str(ANALYSIS_DIR / "keystone_lib"))
sys.path.insert(0, str(ANALYSIS_DIR))
sys.path.insert(0, str(ROOT / "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import build_circuit_scale_follow as release
from circuit_fw_tools import decode_firmware, encode_firmware, load
import circuit_shift_automation_patch as mod

STOCK = Path(r"C:\Users\admin\AI-Projects\circuit-firmware-3592.syx")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def place(image: bytearray, address: int, code: bytes, fill: int, what: str) -> None:
    region = image[mod.image_offset(address) : mod.image_offset(address) + len(code)]
    if any(value != fill for value in region):
        raise SystemExit(f"{what} slot at {address:#010x} is not free")
    image[mod.image_offset(address) : mod.image_offset(address) + len(code)] = code


def build_record_only(lfo_image: bytes) -> bytes:
    image = bytearray(lfo_image)
    place(image, mod.ARM_RECORD_HELPER, mod.build_record_helper(), 0xFF, "record helper")
    mod.patch_exact(
        image,
        mod.RECORD_VALUE_HOOK,
        mod.RECORD_VALUE_STOCK,
        mod.assemble_thumb(f"bl {mod.ARM_RECORD_HELPER:#x}", mod.RECORD_VALUE_HOOK),
    )
    return bytes(image)


def _playback_common(lfo_image: bytes, sites) -> bytes:
    image = bytearray(lfo_image)
    place(image, mod.PLAYBACK_DECODER, mod.build_playback_decoder(), 0, "decoder")
    place(image, mod.SAMPLE_PLAYBACK_HELPER, mod.build_sample_playback_helper(), 0, "sample helper")
    place(image, mod.FILTER_APPLY_HELPER, mod.build_filter_apply_helper(), 0, "filter helper")
    for address, stock in sites:
        mod.patch_exact(
            image, address, stock,
            mod.assemble_thumb(f"bl {mod.PLAYBACK_DECODER:#x}", address),
        )
    return bytes(image)


def _place_one(lfo_image: bytes, which: str) -> bytes:
    """Write exactly one helper into exactly one slot.  Nothing is hooked."""
    image = bytearray(lfo_image)
    if which == "slot_decoder":
        place(image, mod.PLAYBACK_DECODER, mod.build_playback_decoder(), 0, "decoder")
    elif which == "slot_sample":
        place(image, mod.SAMPLE_PLAYBACK_HELPER, mod.build_sample_playback_helper(), 0, "sample")
    else:
        place(image, mod.FILTER_APPLY_HELPER, mod.build_filter_apply_helper(), 0, "filter")
    return bytes(image)


def build_slot_decoder(i):
    return _place_one(i, "slot_decoder")


def build_slot_sample(i):
    return _place_one(i, "slot_sample")


def build_slot_filter(i):
    return _place_one(i, "slot_filter")


def build_slot_divider(lfo_image: bytes) -> bytes:
    """Place the step divider only, hooked to nothing, to prove its new slot."""
    image = bytearray(lfo_image)
    # the LFO layer already placed it when USE_STEP_DIVIDER is on; this target
    # exists to test the slot in isolation, so just return the LFO image which
    # already contains it and nothing else from the automation layer.
    return bytes(image)


def build_helpers_only(lfo_image: bytes) -> bytes:
    """Place the decoder and its two helpers but hook nothing.

    Separates "writing code into these slots is harmful" from "calling it from
    the reload site is harmful".  Nothing here is ever executed, so a brick
    means one of the three slots is live memory rather than dead space.
    """
    return _playback_common(lfo_image, ())


def build_tick_only(lfo_image: bytes) -> bytes:
    """Only the sequencer-tick site.  Never runs at boot; only while playing."""
    return _playback_common(lfo_image, ((mod.PLAYBACK_TICK_PATCH, mod.PLAYBACK_TICK_STOCK),))


def build_reload_only(lfo_image: bytes) -> bytes:
    """Only the pattern-reload site.  This one does run during boot session load."""
    return _playback_common(lfo_image, ((mod.PLAYBACK_RELOAD_PATCH, mod.PLAYBACK_RELOAD_STOCK),))


def build_playback_only(lfo_image: bytes) -> bytes:
    image = bytearray(lfo_image)
    place(image, mod.PLAYBACK_DECODER, mod.build_playback_decoder(), 0, "decoder")
    place(image, mod.SAMPLE_PLAYBACK_HELPER, mod.build_sample_playback_helper(), 0, "sample helper")
    place(image, mod.FILTER_APPLY_HELPER, mod.build_filter_apply_helper(), 0, "filter helper")
    for address, stock in (
        (mod.PLAYBACK_TICK_PATCH, mod.PLAYBACK_TICK_STOCK),
        (mod.PLAYBACK_RELOAD_PATCH, mod.PLAYBACK_RELOAD_STOCK),
    ):
        mod.patch_exact(
            image, address, stock,
            mod.assemble_thumb(f"bl {mod.PLAYBACK_DECODER:#x}", address),
        )
    return bytes(image)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("half", choices=("record", "playback", "tick", "reload", "helpers", "slot_decoder", "slot_sample", "slot_filter", "slot_divider"))
    args = parser.parse_args()

    stock, messages, original = load(STOCK)
    base, _ = release.build_image(stock)
    lfo_image, _ = mod.apply_clean_filter_lfo(base)

    builder = {
        "record": build_record_only,
        "playback": build_playback_only,
        "tick": build_tick_only,
        "reload": build_reload_only,
        "helpers": build_helpers_only,
        "slot_decoder": build_slot_decoder,
        "slot_sample": build_slot_sample,
        "slot_filter": build_slot_filter,
        "slot_divider": build_slot_divider,
    }[args.half]
    patched = builder(lfo_image)

    sysex = encode_firmware(patched, messages)
    decoded, msgs = decode_firmware(sysex)
    if decoded != patched or len(sysex) != len(original):
        raise SystemExit("sysex round-trip failed")
    if any(any(b & 0x80 for b in m.payload) for m in msgs):
        raise SystemExit("non-MIDI-safe byte")

    out = ROOT / "build" / f"circuit-isolate-{args.half}"
    out.mkdir(parents=True, exist_ok=True)
    name = f"circuit-3592-isolate-{args.half}"
    (out / f"{name}.syx").write_bytes(sysex)
    (out / "circuit-3592-stock-recovery.syx").write_bytes(original)

    changed = sum(1 for a, b in zip(lfo_image, patched) if a != b)
    print(f"half           {args.half}")
    print(f"changed vs LFO {changed} bytes")
    print(f"sysex          {sha(sysex)}")
    print(f"files          {out}")


if __name__ == "__main__":
    main()
