"""Build the opt-in Scale Follow modification for Circuit firmware 3592.

The patch is intentionally narrow:

* Both stock Drum Pitch paths are remapped: the four pattern/session reload
  reads and the four live parameter callbacks sent to the DSP.
* Stored CC and automation bytes remain untouched.
* Shift + Scales toggles Scale Follow; normal Scales behavior is preserved.
  Shift is detected from the proven panel-event sequence, not the stock
  held-button query (which is inactive by the time the Scales case runs).
* Mode initializes on after power-up, on the first panel event or Drum Pitch
  read.  A separate initialized bit prevents an intentional mode-off toggle
  from being mistaken for fresh boot state later in the same session.
* The live scale index, root, and mode bit use two verified alignment-padding
  bytes immediately after the stock 174-byte parameter map.  Stock pointer
  handling is not modified.

No output from this script should be flashed until ``verify_build`` succeeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from keystone import KS_ARCH_ARM, KS_MODE_LITTLE_ENDIAN, KS_MODE_THUMB, Ks

from circuit_fw_tools import decode_firmware, encode_firmware, load
from circuit_scale_follow import SCALE_INTERVALS, self_test as mapping_self_test


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80
RELEASE_VERSION = "0.1.0"
STOCK_IMAGE_SHA256 = "1a424d4f116c9b76c3e4e9c1cfa1abb3e262f7d120529c23992e7ee047e1f1ee"
STOCK_SYSEX_SHA256 = "260a72ebd10208aae44f7c01ad18a79cf1d7ad32658ecd1dee0d5215c0e6b7c0"

PARAMETER_MAP = 0x20002D00
PARAMETER_MAP_LENGTH = 0xAE
STATE_SCALE_MODE = PARAMETER_MAP + PARAMETER_MAP_LENGTH
STATE_ROOT = STATE_SCALE_MODE + 1
MODE_BIT = 0x80
SHIFT_ARMED_BIT = 0x40
INITIALIZED_BIT = 0x20
SCALE_MASK = 0x0F

STOCK_PATTERN_PARAMETER_GETTER = 0x08011EA8
STOCK_DRUM_CONTROL_GETTER = 0x0800D8E4
STOCK_MASTER_ENTRY_TARGET = 0x080145CE
STOCK_PARAMETER_FIELD_GET = 0x0801438E
STOCK_SCALE_SELECTION_GET = 0x080105F4
STOCK_SCALE_LIMIT_INIT = 0x08014AA0
STOCK_BUTTON_PRESSED = 0x0800C5A8
STOCK_MAIN_VIEW_SELECT = 0x08019F8C
STOCK_SCALE_MASKS = 0x0802E628
STOCK_DRUM_DIRTY_FLAGS = 0x20002FD3

SHIFT_LOGICAL_ID = 0x1B
SCALES_LOGICAL_ID = 0x12

CAVE_START = 0x0802D408
CAVE_END = 0x0802D701

MAIN_HANDLER_ENTRY = 0x0801DDFC
MAIN_HANDLER_RESUME = 0x0801DE00

UPDATE_ROOT_ENTRY = 0x0802D408
UPDATE_SCALE = 0x0802D468
TOGGLE_SCALES_CASE = 0x0802D4C0
MAPPED_PITCH_GETTER = 0x0802D520
MAPPED_PATTERN_PITCH_GETTER = 0x0802D5E4
REFRESH_ALL_DRUM_PITCH = 0x0802D600
EVENT_HOOK_ENTRY = 0x0802D610
SCALE_COUNTS = 0x0802D680
UI_SCALE_COMMIT = 0x0802D690
UI_ROOT_COMMIT = 0x0802D694
UI_COMMIT_COMMON = 0x0802D698


@dataclass(frozen=True)
class PatchSite:
    address: int
    stock: bytes
    assembly: str
    purpose: str


PATCH_SITES = (
    PatchSite(
        MAIN_HANDLER_ENTRY,
        bytes.fromhex("f8 b5 04 46"),
        f"b.w {EVENT_HOOK_ENTRY:#x}",
        "arm Shift from the proven main panel-event sequence",
    ),
    PatchSite(
        0x0801C924,
        bytes.fromhex("f7 f7 53 fe"),
        f"bl {UPDATE_ROOT_ENTRY:#x}",
        "capture live master root through a stock-equivalent entry wrapper",
    ),
    PatchSite(
        0x0801C940,
        bytes.fromhex("f8 f7 ae f8"),
        f"bl {UPDATE_SCALE:#x}",
        "mirror live master-scale changes into reserved padding state",
    ),
    PatchSite(
        0x0801062A,
        bytes.fromhex("03 f0 d0 bf"),
        f"b.w {UI_SCALE_COMMIT:#x}",
        "refresh drum pitch after the real held-Scales scale commit completes",
    ),
    PatchSite(
        0x08014F5C,
        bytes.fromhex("ff f7 37 bb"),
        f"b.w {UI_ROOT_COMMIT:#x}",
        "refresh drum pitch after the mapped-pad root commit completes",
    ),
    PatchSite(
        0x08014F7E,
        bytes.fromhex("ff f7 26 bb"),
        f"b.w {UI_ROOT_COMMIT:#x}",
        "refresh drum pitch after the direct root commit completes",
    ),
    PatchSite(
        0x0801DF6E,
        bytes.fromhex("00 21 04 23"),
        f"b.w {TOGGLE_SCALES_CASE:#x}",
        "make Shift + Scales toggle Scale Follow",
    ),
    PatchSite(
        0x080169D4,
        bytes.fromhex("fb f7 68 fa"),
        f"bl {MAPPED_PATTERN_PITCH_GETTER:#x}",
        "map Drum 1 Pitch when stock pattern/session state is loaded",
    ),
    PatchSite(
        0x08016D6A,
        bytes.fromhex("fb f7 9d f8"),
        f"bl {MAPPED_PATTERN_PITCH_GETTER:#x}",
        "map Drum 2 Pitch when stock pattern/session state is loaded",
    ),
    PatchSite(
        0x080193B0,
        bytes.fromhex("f8 f7 7a fd"),
        f"bl {MAPPED_PATTERN_PITCH_GETTER:#x}",
        "map Drum 3 Pitch when stock pattern/session state is loaded",
    ),
    PatchSite(
        0x080194AC,
        bytes.fromhex("f8 f7 fc fc"),
        f"bl {MAPPED_PATTERN_PITCH_GETTER:#x}",
        "map Drum 4 Pitch when stock pattern/session state is loaded",
    ),
    PatchSite(
        0x08018B10,
        bytes.fromhex("f4 f7 e8 fe"),
        f"bl {MAPPED_PITCH_GETTER:#x}",
        "map live Drum 1 Pitch before its stock DSP conversion",
    ),
    PatchSite(
        0x0801A16A,
        bytes.fromhex("f3 f7 bb fb"),
        f"bl {MAPPED_PITCH_GETTER:#x}",
        "map live Drum 2 Pitch before its stock DSP conversion",
    ),
    PatchSite(
        0x08019F0A,
        bytes.fromhex("f3 f7 eb fc"),
        f"bl {MAPPED_PITCH_GETTER:#x}",
        "map live Drum 3 Pitch before its stock DSP conversion",
    ),
    PatchSite(
        0x08019E72,
        bytes.fromhex("f3 f7 37 fd"),
        f"bl {MAPPED_PITCH_GETTER:#x}",
        "map live Drum 4 Pitch before its stock DSP conversion",
    ),
)


CODE_SECTIONS = (
    (
        UPDATE_ROOT_ENTRY,
        UPDATE_SCALE,
        f"""
            push {{r1, r2, r3, r4, lr}}
            sub sp, #4
            bl {STOCK_MASTER_ENTRY_TARGET:#x}
            str r0, [sp]

            ldr r0, [r4, #0x58]
            bl {STOCK_SCALE_SELECTION_GET:#x}
            ldrb r1, [r0]
            and r1, r1, #{SCALE_MASK:#x}
            ldr r2, ={STATE_SCALE_MODE:#x}
            ldrb r3, [r2]
            and r3, r3, #{MODE_BIT | SHIFT_ARMED_BIT | INITIALIZED_BIT:#x}
            orrs r3, r1
            strb r3, [r2]

            ldr r1, [r4, #0x58]
            ldr r2, [r1, #4]
            and r2, r2, #1
            adds r2, #2
            add.w r1, r1, r2, lsl #2
            ldr r1, [r1, #4]
            ldrb r1, [r1]
            ldr r2, ={STATE_ROOT:#x}
            strb r1, [r2]
            ldr r2, ={STATE_SCALE_MODE:#x}
            ldrb r1, [r2]
            ldr r0, [sp]
            tst r1, #{MODE_BIT:#x}
            beq root_update_done
            mov r4, r0
            bl {REFRESH_ALL_DRUM_PITCH:#x}
            mov r0, r4
        root_update_done:
            add sp, #4
            pop {{r1, r2, r3, r4, pc}}
        """,
        "stock-equivalent master-entry wrapper and raw-root capture",
    ),
    (
        UPDATE_SCALE,
        TOGGLE_SCALES_CASE,
        f"""
            push {{r1, r2, r3, r4, lr}}
            sub sp, #4
            bl {STOCK_SCALE_LIMIT_INIT:#x}
            ldr r2, [sp, #8]
            ldr r3, ={STATE_SCALE_MODE:#x}
            ldrb r1, [r3]
            and r1, r1, #{MODE_BIT | INITIALIZED_BIT:#x}
            orrs r1, r2
            strb r1, [r3]
            tst r1, #{MODE_BIT:#x}
            beq scale_update_done
            mov r4, r0
            bl {REFRESH_ALL_DRUM_PITCH:#x}
            mov r0, r4
        scale_update_done:
            add sp, #4
            pop {{r1, r2, r3, r4, pc}}
        """,
        "master-scale state mirror",
    ),
    (
        TOGGLE_SCALES_CASE,
        MAPPED_PITCH_GETTER,
        f"""
            ldr r0, ={STATE_SCALE_MODE:#x}
            ldrb r1, [r0]
            tst r1, #{SHIFT_ARMED_BIT:#x}
            beq open_scales_view
            bic r1, r1, #{SHIFT_ARMED_BIT:#x}
            eor r1, r1, #{MODE_BIT:#x}
            strb r1, [r0]
            bl {REFRESH_ALL_DRUM_PITCH:#x}
            pop {{r3, r4, r5, r6, r7, pc}}
        open_scales_view:
            mov r0, r4
            movs r1, #4
            movs r2, #{SCALES_LOGICAL_ID}
            bl {STOCK_MAIN_VIEW_SELECT:#x}
            pop {{r3, r4, r5, r6, r7, pc}}
        """,
        "Shift + Scales mode toggle and stock Scales fallback",
    ),
    (
        MAPPED_PITCH_GETTER,
        MAPPED_PATTERN_PITCH_GETTER,
        f"""
            push {{r1, r4, r5, r6, r7, lr}}
            bl {STOCK_DRUM_CONTROL_GETTER:#x}
            mov r4, r0
            ldr r6, ={STATE_SCALE_MODE:#x}
            ldrb r5, [r6]
            tst r5, #{INITIALIZED_BIT:#x}
            bne mapped_mode_ready
            orr r5, r5, #{MODE_BIT | INITIALIZED_BIT:#x}
            strb r5, [r6]
        mapped_mode_ready:
            tst r5, #{MODE_BIT:#x}
            beq mapped_pitch_done

            and r1, r5, #{SCALE_MASK:#x}
            ldr r7, ={STATE_ROOT:#x}
            ldrb r7, [r7]
        normalize_root:
            cmp r7, #12
            blo root_ready
            subs r7, #12
            b normalize_root
        root_ready:
            ldr r2, ={SCALE_COUNTS:#x}
            ldrb r6, [r2, r1]
            ldr r2, ={STOCK_SCALE_MASKS:#x}
            ldr.w r5, [r2, r1, lsl #2]

            cmp r4, #64
            bhs degree_positive
            rsb r0, r4, #64
            lsls r1, r6, #1
            mul r0, r0, r1
            adds r0, #32
            lsrs r0, r0, #6
            rsbs r4, r0, #0
            b degree_ready

        degree_positive:
            subs r0, r4, #64
            lsls r1, r6, #1
            mul r0, r0, r1
            adds r0, #31
            movs r4, #0
        divide_by_63:
            cmp r0, #63
            blo degree_ready
            subs r0, #63
            adds r4, #1
            b divide_by_63

        degree_ready:
            movs r0, #0
            mov r1, r4
            cmp r1, #0
            bge reduce_positive_degree
        reduce_negative_degree:
            adds r1, r6
            subs r0, #1
            cmp r1, #0
            blt reduce_negative_degree
            b degree_reduced
        reduce_positive_degree:
            cmp r1, r6
            blo degree_reduced
            subs r1, r6
            adds r0, #1
            b reduce_positive_degree

        degree_reduced:
            movs r2, #0
        find_scale_interval:
            ands r3, r5, #3
            bne next_pitch_class
            cmp r1, #0
            beq scale_interval_found
            subs r1, #1
        next_pitch_class:
            lsrs r5, r5, #2
            adds r2, #1
            cmp r2, #12
            blo find_scale_interval

        scale_interval_found:
            movs r3, #12
            mul r0, r0, r3
            adds r0, r2
            adds r0, r7
            adds r0, #64
            b mapped_pitch_return

        mapped_pitch_done:
            mov r0, r4
        mapped_pitch_return:
            pop {{r1, r4, r5, r6, r7, pc}}
        """,
        "four-octave master-scale drum-pitch mapper",
    ),
    (
        MAPPED_PATTERN_PITCH_GETTER,
        REFRESH_ALL_DRUM_PITCH,
        f"""
            push {{r1, r4, r5, r6, r7, lr}}
            bl {STOCK_PATTERN_PARAMETER_GETTER:#x}
            b {MAPPED_PITCH_GETTER + 6:#x}
        """,
        "pattern/session reload entry for the shared pitch mapper",
    ),
    (
        REFRESH_ALL_DRUM_PITCH,
        EVENT_HOOK_ENTRY,
        f"""
            ldr r0, ={STOCK_DRUM_DIRTY_FLAGS:#x}
            ldrb r1, [r0]
            orr r1, r1, #0x0f
            strb r1, [r0]
            bx lr
        """,
        "request all four stock Drum callbacks through the dirty-bit scheduler",
    ),
    (
        EVENT_HOOK_ENTRY,
        SCALE_COUNTS,
        f"""
            push {{r3, r4, r5, r6, r7, lr}}
            push {{r0, r1, r2}}
            ldr r2, ={STATE_SCALE_MODE:#x}
            ldrb r0, [r2]
            tst r0, #{INITIALIZED_BIT:#x}
            bne panel_mode_initialized
            orr r0, r0, #{MODE_BIT | INITIALIZED_BIT:#x}
            strb r0, [r2]
            mov r3, r1
            bl {REFRESH_ALL_DRUM_PITCH:#x}
            mov r1, r3
        panel_mode_initialized:
            cmp r1, #{SHIFT_LOGICAL_ID}
            beq arm_shift_event
            cmp r1, #{SCALES_LOGICAL_ID}
            beq panel_event_done

            ldrb r0, [r2]
            bic r0, r0, #{SHIFT_ARMED_BIT:#x}
            strb r0, [r2]
            b panel_event_done

        arm_shift_event:
            ldrb r0, [r2]
            orr r0, r0, #{SHIFT_ARMED_BIT:#x}
            strb r0, [r2]

        panel_event_done:
            pop {{r0, r1, r2}}
            mov r4, r0
            b.w {MAIN_HANDLER_RESUME:#x}
        """,
        "arm Shift and preserve it only for the immediately following Scales event",
    ),
    (
        UI_SCALE_COMMIT,
        UI_ROOT_COMMIT,
        f"""
            movs r1, #0
            b {UI_COMMIT_COMMON:#x}
        """,
        "held-Scales scale-commit entry",
    ),
    (
        UI_ROOT_COMMIT,
        UI_COMMIT_COMMON,
        f"""
            movs r1, #1
            b {UI_COMMIT_COMMON:#x}
        """,
        "held-Scales root-commit entry",
    ),
    (
        UI_COMMIT_COMMON,
        CAVE_END,
        f"""
            push {{r4, r5, r6, lr}}
            mov r4, r0
            mov r6, r1
            bl {STOCK_MASTER_ENTRY_TARGET:#x}
            mov r5, r0
            cbnz r6, ui_root_committed

            mov r0, r4
            bl {STOCK_SCALE_SELECTION_GET:#x}
            ldrb r1, [r0]
            and r1, r1, #{SCALE_MASK:#x}
            ldr r2, ={STATE_SCALE_MODE:#x}
            ldrb r3, [r2]
            and r3, r3, #{MODE_BIT | INITIALIZED_BIT:#x}
            orrs r3, r1
            strb r3, [r2]
            b ui_commit_check_mode

        ui_root_committed:
            ldr r1, [r4, #4]
            and r1, r1, #1
            adds r1, #2
            add.w r1, r4, r1, lsl #2
            ldr r1, [r1, #4]
            ldrb r1, [r1]
            ldr r2, ={STATE_SCALE_MODE:#x}
            strb r1, [r2, #1]

        ui_commit_check_mode:
            ldrb r1, [r2]
            tst r1, #{MODE_BIT:#x}
            beq ui_commit_done
            bl {REFRESH_ALL_DRUM_PITCH:#x}
        ui_commit_done:
            mov r0, r5
            pop {{r4, r5, r6, pc}}
        """,
        "post-commit capture and refresh for the real held-Scales UI handlers",
    ),
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def offset(address: int) -> int:
    result = address - BASE
    if not 0 <= result < IMAGE_SIZE:
        raise ValueError(f"address outside firmware image: {address:#010x}")
    return result


def assemble(source: str, address: int) -> bytes:
    assembler = Ks(KS_ARCH_ARM, KS_MODE_THUMB | KS_MODE_LITTLE_ENDIAN)
    encoding, _ = assembler.asm(source, address)
    if encoding is None:
        raise ValueError(f"assembler produced no bytes at {address:#010x}")
    return bytes(encoding)


def patch_bytes(image: bytearray, address: int, expected: bytes, replacement: bytes) -> None:
    start = offset(address)
    actual = bytes(image[start : start + len(expected)])
    if actual != expected:
        raise ValueError(
            f"stock signature mismatch at {address:#010x}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )
    if len(replacement) != len(expected):
        raise ValueError(
            f"replacement at {address:#010x} has {len(replacement)} bytes, "
            f"expected {len(expected)}"
        )
    image[start : start + len(expected)] = replacement


def build_image(stock: bytes) -> tuple[bytes, dict]:
    if len(stock) != IMAGE_SIZE:
        raise ValueError(f"unexpected image size {len(stock):#x}")
    if sha256(stock) != STOCK_IMAGE_SHA256:
        raise ValueError("canonical image hash does not match verified firmware 3592")
    if any(stock[offset(CAVE_START) : offset(CAVE_END)]):
        raise ValueError("selected code cave is not entirely zero-filled")

    mapping_self_test()
    image = bytearray(stock)
    sections = []

    for start, end, source, purpose in CODE_SECTIONS:
        code = assemble(source, start)
        if len(code) > end - start:
            raise ValueError(
                f"{purpose} is {len(code)} bytes but its slot has only {end - start}"
            )
        image[offset(start) : offset(start) + len(code)] = code
        sections.append(
            {
                "address": f"{start:#010x}",
                "length": len(code),
                "slot_length": end - start,
                "purpose": purpose,
            }
        )

    counts = bytes(len(intervals) for intervals in SCALE_INTERVALS)
    image[offset(SCALE_COUNTS) : offset(SCALE_COUNTS) + len(counts)] = counts
    sections.append(
        {
            "address": f"{SCALE_COUNTS:#010x}",
            "length": len(counts),
            "slot_length": len(counts),
            "purpose": "notes-per-octave table for the 16 stock scales",
        }
    )

    sites = []
    for site in PATCH_SITES:
        replacement = assemble(site.assembly, site.address)
        patch_bytes(image, site.address, site.stock, replacement)
        sites.append(
            {
                "address": f"{site.address:#010x}",
                "stock": site.stock.hex(),
                "patched": replacement.hex(),
                "purpose": site.purpose,
            }
        )

    manifest = {
        "project": "Circuit Scale Follow",
        "release": RELEASE_VERSION,
        "target": "original Novation Circuit firmware 1.8 build 3592",
        "base": f"{BASE:#010x}",
        "stock_sysex_sha256": STOCK_SYSEX_SHA256,
        "stock_image_sha256": STOCK_IMAGE_SHA256,
        "patched_image_sha256": sha256(image),
        "mode_default": "on after one-time lazy RAM initialization",
        "state_storage": (
            "two alignment-padding bytes after the stock 0xAE-byte "
            "parameter map; stock callback pointers are untouched"
        ),
        "pitch_paths": (
            "four stock pattern/session reload reads plus four live Drum "
            "parameter callbacks"
        ),
        "toggle": "Shift + Scales",
        "shift_detection": (
            "main panel-event ID 0x1B arms bit 0x40; Scales consumes it; "
            "any other panel event clears it"
        ),
        "initialization": (
            "bit 0x20 marks initialized state; the first panel event or Drum "
            "Pitch read sets mode-on plus initialized, while later explicit "
            "mode-off state remains off"
        ),
        "reference_pitch": "loaded sample at C when stock pitch is 64",
        "range": "two scale octaves below through two scale octaves above the master tonic",
        "code_sections": sections,
        "patch_sites": sites,
    }
    return bytes(image), manifest


def verify_build(stock: bytes, patched: bytes, sysex: bytes, manifest: dict) -> dict:
    if len(patched) != len(stock):
        raise ValueError("patched image size changed")
    if patched == stock:
        raise ValueError("patched image is identical to stock")

    changed = {index for index, (left, right) in enumerate(zip(stock, patched)) if left != right}
    allowed = set(range(offset(CAVE_START), offset(CAVE_END)))
    for site in PATCH_SITES:
        allowed.update(range(offset(site.address), offset(site.address) + len(site.stock)))
    unexpected = sorted(changed - allowed)
    if unexpected:
        raise ValueError(f"unexpected changed image offsets: {unexpected[:16]}")

    decoded, messages = decode_firmware(sysex)
    if decoded != patched:
        raise ValueError("re-decoded output SysEx does not match the patched image")
    if any(any(byte & 0x80 for byte in message.payload) for message in messages):
        raise ValueError("output SysEx contains a non-MIDI-safe payload byte")

    return {
        "changed_image_bytes": len(changed),
        "unexpected_changed_bytes": len(unexpected),
        "sysex_message_count": len(messages),
        "sysex_sha256": sha256(sysex),
        "patched_image_sha256": manifest["patched_image_sha256"],
        "mapping_tests": "passed for 16 scales x 12 roots x 128 CC values",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_sysex", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("build") / "circuit-scale-follow",
    )
    args = parser.parse_args()

    stock, messages, original_sysex = load(args.stock_sysex)
    if sha256(original_sysex) != STOCK_SYSEX_SHA256:
        raise ValueError("input SysEx hash does not match verified firmware 3592")

    patched, manifest = build_image(stock)
    rebuilt_sysex = encode_firmware(patched, messages)
    if len(rebuilt_sysex) != len(original_sysex):
        raise ValueError("repacked SysEx size changed")

    verification = verify_build(stock, patched, rebuilt_sysex, manifest)
    manifest["verification"] = verification

    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_path = args.output_dir / "circuit-3592-scale-follow.bin"
    stock_image_path = args.output_dir / "circuit-3592-stock.bin"
    sysex_path = args.output_dir / "circuit-3592-scale-follow.syx"
    recovery_path = args.output_dir / "circuit-3592-stock-recovery.syx"
    manifest_path = args.output_dir / "circuit-3592-scale-follow-manifest.json"
    image_path.write_bytes(patched)
    stock_image_path.write_bytes(stock)
    sysex_path.write_bytes(rebuilt_sysex)
    recovery_path.write_bytes(original_sysex)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"patched image: {image_path}")
    print(f"decoded stock image: {stock_image_path}")
    print(f"patched SysEx: {sysex_path}")
    print(f"stock recovery SysEx: {recovery_path}")
    print(f"manifest: {manifest_path}")
    print(json.dumps(verification, indent=2))


if __name__ == "__main__":
    main()
