"""Build the hardware-validated SysEx for tagged Shift automation.

    python experimental\\build_circuit_shift_automation.py circuit-firmware-3592.syx

Layers, in order, on the verified v0.4.2 base:
  1. the Filter LFO reconstruction with active-low stock Clear reset
  2. tagged Shift automation record and playback

Run experimental\\verify_circuit_shift_automation.py first; this script refuses to
write anything if the staged hashes do not match what the verifier saw.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parent
ROOT = ANALYSIS_DIR.parent
PUBLISHED_TOOLS = ROOT / "tools"

sys.path.insert(0, str(ANALYSIS_DIR / "keystone_lib"))
sys.path.insert(0, str(ANALYSIS_DIR))
sys.path.insert(0, str(PUBLISHED_TOOLS))
sys.stdout.reconfigure(encoding="utf-8")

import build_circuit_scale_follow as release
import circuit_shift_automation_patch as mod
from circuit_fw_tools import decode_firmware, encode_firmware, load

V042_IMAGE = "e6a2b77bea0918e28b2cdadca6bef96654d6a39d7272b3a1f9ac25a51815cca2"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stock_sysex",
        type=Path,
        help="legitimate stock Circuit 1.8 build 3592 update SysEx",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "build" / "circuit-shift-automation"
    )
    args = parser.parse_args()

    stock, messages, original = load(args.stock_sysex)
    if mod.sha256(original) != release.STOCK_SYSEX_SHA256:
        raise SystemExit("input SysEx hash does not match verified firmware 3592")

    base, _ = release.build_image(stock)
    if mod.sha256(base) != V042_IMAGE:
        raise SystemExit("v0.4.2 base does not match the verified image")

    lfo_image, lfo_manifest = mod.apply_clean_filter_lfo(base)
    patched, auto_manifest = mod.apply_shift_automation(lfo_image)

    sysex = encode_firmware(patched, messages)
    if len(sysex) != len(original):
        raise SystemExit("repacked SysEx size changed")
    decoded, decoded_messages = decode_firmware(sysex)
    if decoded != patched:
        raise SystemExit("re-decoded SysEx does not match the patched image")
    if any(any(b & 0x80 for b in m.payload) for m in decoded_messages):
        raise SystemExit("output SysEx contains a non-MIDI-safe payload byte")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = "circuit-3592-shift-automation-test"
    (args.output_dir / f"{stem}.bin").write_bytes(patched)
    (args.output_dir / f"{stem}.syx").write_bytes(sysex)
    (args.output_dir / "circuit-3592-stock-recovery.syx").write_bytes(original)
    (args.output_dir / f"{stem}-manifest.json").write_text(
        json.dumps(
            {
                "status": "hardware validated 2026-07-31, NOT published",
                "hardware_validation": {
                    "boot": "normal",
                    "filter_shift_automation": (
                        "recorded LFO state replays without a knob nudge"
                    ),
                    "clear_clockwise": (
                        "blue stock reset disables the selected drum LFO"
                    ),
                    "clear_counter_clockwise": (
                        "red stock automation erase leaves the LFO active"
                    ),
                },
                "base_image_sha256": mod.sha256(base),
                "clean_lfo_image_sha256": mod.sha256(lfo_image),
                "patched_image_sha256": mod.sha256(patched),
                "sysex_sha256": mod.sha256(sysex),
                "clean_lfo": lfo_manifest,
                "shift_automation": auto_manifest,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"image  {mod.sha256(patched)}")
    print(f"sysex  {mod.sha256(sysex)}")
    print(f"files  {args.output_dir}")


if __name__ == "__main__":
    main()
