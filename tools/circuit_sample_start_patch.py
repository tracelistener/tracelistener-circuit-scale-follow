"""Verified Shift+Macro 3/4 sample-start stage for original Circuit 3592.

This stage accepts only the exact Scale Follow + isolated Drum Distortion
Select image.  It keeps normal decay intact and applies a per-drum sample
displacement only when a new voice is triggered.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from keystone import KS_ARCH_ARM, KS_MODE_LITTLE_ENDIAN, KS_MODE_THUMB, Ks


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80
INPUT_IMAGE_SHA256 = "8417a464ad5f6d5ac33e4b345a013ba016583221772a625b208710392ce9856b"
OUTPUT_IMAGE_SHA256 = "e6a2b77bea0918e28b2cdadca6bef96654d6a39d7272b3a1f9ac25a51815cca2"

STOCK_DRUM_CONTROL_GETTER = 0x0800D8E4
STOCK_BUTTON_PRESSED = 0x0800C5A8
DSP_SETTER = 0x0801633C
DSP_GETTER = 0x08016E64
PARAMETER_CONTEXT_POINTER = 0x20002DB0
SHIFT_LOGICAL_ID = 0x1B

ARM_CAVE_START = 0x08025594
ARM_CAVE_END = 0x08025694
PATCH_ARM_CAVE_START = 0x080367E4
PATCH_ARM_CAVE_END = 0x0803684D
ENCODER_CACHE_HELPER_START = 0x08036820

DSP_PROGRAM_DATA = 0x08025B6E + 6
DSP_HOOK_PC = 0x2720
DSP_HOOK_ADDRESS = DSP_PROGRAM_DATA + DSP_HOOK_PC * 3
DSP_RESUME_PC = 0x2725
DSP_TRAMPOLINE_PC = 0x00B4
DSP_TRAMPOLINE_ADDRESS = DSP_PROGRAM_DATA + DSP_TRAMPOLINE_PC * 3
DSP_CAVE_END_PC = 0x0100
DSP_CAVE_END_ADDRESS = DSP_PROGRAM_DATA + DSP_CAVE_END_PC * 3

STOCK_DESCRIPTOR_COPY = bytes.fromhex("44 D8 00 02 03 84 44 D8 00 02 03 C4")

# Stock DSP code does not access X:$1E59..$1E64.  Four words hold raw offsets,
# four hold precomputed address displacements, and four hold normal decay plus
# the packed previous Shift encoder value.
OFFSET_RAW_BASE = 0x1E59
OFFSET_DISPLACEMENT_BASE = 0x1E5D
DECAY_CACHE_BASE = 0x1E61


@dataclass(frozen=True)
class Hook:
    address: int
    stock: bytes
    purpose: str


HOOKS = (
    Hook(0x08018B00, bytes.fromhex("F4 F7 F0 FE"), "Drum 1 patch getter"),
    Hook(0x08018B34, bytes.fromhex("F4 F7 D6 FE"), "Drum 1 decay getter"),
    Hook(0x0801A15A, bytes.fromhex("F3 F7 C3 FB"), "Drum 2 patch getter"),
    Hook(0x0801A18E, bytes.fromhex("F3 F7 A9 FB"), "Drum 2 decay getter"),
    Hook(0x08019EFA, bytes.fromhex("F3 F7 F3 FC"), "Drum 3 patch getter"),
    Hook(0x08019F2E, bytes.fromhex("F3 F7 D9 FC"), "Drum 3 decay getter"),
    Hook(0x08019E62, bytes.fromhex("F3 F7 3F FD"), "Drum 4 patch getter"),
    Hook(0x08019E96, bytes.fromhex("F3 F7 25 FD"), "Drum 4 decay getter"),
)


def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def image_offset(address: int) -> int:
    value = address - BASE
    if not 0 <= value < IMAGE_SIZE:
        raise ValueError(f"address outside firmware image: {address:#010x}")
    return value


def assemble_thumb(source: str, address: int) -> bytes:
    assembler = Ks(KS_ARCH_ARM, KS_MODE_THUMB | KS_MODE_LITTLE_ENDIAN)
    encoding, _ = assembler.asm(source, address)
    if encoding is None:
        raise ValueError(f"assembler produced no bytes at {address:#010x}")
    return bytes(encoding)


def dsp_bytes(words: list[int]) -> bytes:
    if any(not 0 <= word <= 0xFFFFFF for word in words):
        raise ValueError("DSP word outside the 24-bit range")
    return b"".join(word.to_bytes(3, "big") for word in words)


def dsp_trampoline_words() -> list[int]:
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []
    words: list[int] = []

    def mark(name: str) -> None:
        labels[name] = DSP_TRAMPOLINE_PC + len(words)

    def emit(*values: int) -> None:
        words.extend(values)

    def branch(opcode: int, label: str) -> None:
        emit(opcode, 0)
        fixups.append((len(words) - 1, label))

    emit(
        0x44D800,
        0x020384,
        0x44D800,
        0x0203C4,
        0x45D800,
        0x224E00,
        0x0140C5,
        0x00009F,
    )
    branch(0x0AF0AA, "load_d1")
    emit(0x0140C5, 0x0000A9)
    branch(0x0AF0AA, "load_d2")
    emit(0x0140C5, 0x0000B3)
    branch(0x0AF0AA, "load_d3")
    mark("load_d4")
    emit(0x57F000, OFFSET_DISPLACEMENT_BASE + 3)
    branch(0x0AF080, "apply")
    mark("load_d1")
    emit(0x57F000, OFFSET_DISPLACEMENT_BASE)
    branch(0x0AF080, "apply")
    mark("load_d2")
    emit(0x57F000, OFFSET_DISPLACEMENT_BASE + 1)
    branch(0x0AF080, "apply")
    mark("load_d3")
    emit(0x57F000, OFFSET_DISPLACEMENT_BASE + 2)
    mark("apply")
    emit(
        0x02039E,
        0x200010,
        0x02038E,
        0x208E00,
        0x200014,
        0x0203CE,
        0x0AF080,
        DSP_RESUME_PC,
    )
    for index, label in fixups:
        words[index] = labels[label]
    return words


def arm_control_code() -> bytes:
    return assemble_thumb(
        f"""
            push {{r4, r5, r6, r7, r8, lr}}
            mov r4, r1
            bl {STOCK_DRUM_CONTROL_GETTER:#x}
            mov r5, r0
            mov r8, r0
            movs r6, #0
            cmp r4, #7
            blo drum_index_ready
            adds r6, #1
            cmp r4, #14
            blo drum_index_ready
            adds r6, #1
            cmp r4, #21
            blo drum_index_ready
            adds r6, #1
        drum_index_ready:
            cmp r4, #0
            beq patch_changed
            cmp r4, #7
            beq patch_changed
            cmp r4, #14
            beq patch_changed
            cmp r4, #21
            beq patch_changed

            movs r0, #{SHIFT_LOGICAL_ID}
            bl {STOCK_BUTTON_PRESSED:#x}
            cmp r0, #0
            beq normal_decay

            movw r1, #{DECAY_CACHE_BASE:#x}
            adds r1, r1, r6
            mov r0, r1
            bl {DSP_GETTER:#x}
            lsrs r7, r0, #24
            lsls r1, r0, #8
            bmi previous_shift_value_valid
            subs r5, r5, r7
            b shift_delta_ready
        previous_shift_value_valid:
            ubfx r1, r0, #16, #7
            subs r5, r5, r1
        shift_delta_ready:
            movw r1, #{OFFSET_RAW_BASE:#x}
            adds r1, r1, r6
            mov r0, r1
            bl {DSP_GETTER:#x}
            lsrs r0, r0, #24
            lsls r5, r5, #1
            adds r5, r5, r0
            cmp r5, #0
            bge offset_nonnegative
            movs r5, #0
        offset_nonnegative:
            cmp r5, #127
            ble offset_ready
            movs r5, #127
        offset_ready:
            mov r0, r5
            lsls r0, r0, #24
            movw r1, #{OFFSET_RAW_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}
            bl {ENCODER_CACHE_HELPER_START:#x}

            movs r1, #0x9f
            movs r2, #10
            muls r2, r6, r2
            adds r1, r1, r2
            mov r0, r1
            bl {DSP_GETTER:#x}
            lsrs r0, r0, #8
            lsls r0, r0, #2
            movw r1, #0x1d0e
            adds r1, r1, r0
            mov r0, r1
            bl {DSP_GETTER:#x}
            cmp r5, #127
            beq displacement_at_end
            lsrs r0, r0, #7
            muls r0, r5, r0
            b displacement_ready
        displacement_at_end:
            subs r0, r0, #1
        displacement_ready:
            movw r1, #{OFFSET_DISPLACEMENT_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}

            ldr r0, ={PARAMETER_CONTEXT_POINTER:#x}
            ldr r0, [r0]
            ldr r0, [r0, #0x318]
            ldr r1, [r0, #4]
            add.w r1, r1, r4, lsl #3
            ldrh r1, [r1]
            ldr r0, [r0, #0x0c]
            ldr r0, [r0]
            strb r7, [r0, r1]
            mov r0, r7
            b done

        normal_decay:
            mov r0, r5
            lsls r0, r0, #24
            movw r1, #{DECAY_CACHE_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}
            mov r0, r5
            b done
        patch_changed:
            b.w {PATCH_ARM_CAVE_START:#x}
        done:
            pop {{r4, r5, r6, r7, r8, pc}}
        """,
        ARM_CAVE_START,
    )


def arm_patch_code() -> bytes:
    return assemble_thumb(
        f"""
            movw r1, #{OFFSET_RAW_BASE:#x}
            adds r1, r1, r6
            mov r0, r1
            bl {DSP_GETTER:#x}
            lsrs r7, r0, #24
            mov r0, r5
            lsls r0, r0, #2
            movw r1, #0x1d0e
            adds r1, r1, r0
            mov r0, r1
            bl {DSP_GETTER:#x}
            lsrs r0, r0, #7
            muls r0, r7, r0
            movw r1, #{OFFSET_DISPLACEMENT_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}
            mov r0, r5
            pop {{r4, r5, r6, r7, r8, pc}}
        """,
        PATCH_ARM_CAVE_START,
    )


def arm_encoder_cache_helper_code() -> bytes:
    return assemble_thumb(
        f"""
            push {{lr}}
            mov r0, r7
            lsls r0, r0, #8
            orr r0, r0, r8
            movs r2, #0x80
            orrs r0, r0, r2
            lsls r0, r0, #16
            movw r1, #{DECAY_CACHE_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}
            pop {{pc}}
        """,
        ENCODER_CACHE_HELPER_START,
    )


def patch_exact(image: bytearray, address: int, stock: bytes, replacement: bytes) -> None:
    if len(stock) != len(replacement):
        raise ValueError(f"patch size mismatch at {address:#010x}")
    start = image_offset(address)
    actual = bytes(image[start : start + len(stock)])
    if actual != stock:
        raise ValueError(
            f"signature mismatch at {address:#010x}: expected {stock.hex(' ')}, got {actual.hex(' ')}"
        )
    image[start : start + len(replacement)] = replacement


def sample_start_allowed_offsets() -> set[int]:
    arm = arm_control_code()
    patch_arm = arm_patch_code()
    helper = arm_encoder_cache_helper_code()
    dsp = dsp_bytes(dsp_trampoline_words())
    allowed = set(range(image_offset(ARM_CAVE_START), image_offset(ARM_CAVE_START) + len(arm)))
    allowed.update(
        range(image_offset(PATCH_ARM_CAVE_START), image_offset(PATCH_ARM_CAVE_START) + len(patch_arm))
    )
    allowed.update(
        range(image_offset(ENCODER_CACHE_HELPER_START), image_offset(ENCODER_CACHE_HELPER_START) + len(helper))
    )
    allowed.update(
        range(image_offset(DSP_TRAMPOLINE_ADDRESS), image_offset(DSP_TRAMPOLINE_ADDRESS) + len(dsp))
    )
    allowed.update(range(image_offset(DSP_HOOK_ADDRESS), image_offset(DSP_HOOK_ADDRESS) + 12))
    for hook in HOOKS:
        allowed.update(range(image_offset(hook.address), image_offset(hook.address) + 4))
    return allowed


def apply_sample_start_control(base: bytes) -> tuple[bytes, dict]:
    if len(base) != IMAGE_SIZE or sha256(base) != INPUT_IMAGE_SHA256:
        raise ValueError("sample-start base is not the exact verified 0.3.0 image")
    if any(base[image_offset(ARM_CAVE_START) : image_offset(ARM_CAVE_END)]):
        raise ValueError("sample-start primary ARM cave is not zero")
    if any(base[image_offset(PATCH_ARM_CAVE_START) : image_offset(PATCH_ARM_CAVE_END)]):
        raise ValueError("sample-start secondary ARM cave is not zero")
    if any(base[image_offset(DSP_TRAMPOLINE_ADDRESS) : image_offset(DSP_CAVE_END_ADDRESS)]):
        raise ValueError("sample-start DSP cave is not zero")

    arm = arm_control_code()
    patch_arm = arm_patch_code()
    helper = arm_encoder_cache_helper_code()
    dsp = dsp_bytes(dsp_trampoline_words())
    if len(arm) > ARM_CAVE_END - ARM_CAVE_START:
        raise ValueError("sample-start control exceeds primary ARM cave")
    if PATCH_ARM_CAVE_START + len(patch_arm) > ENCODER_CACHE_HELPER_START:
        raise ValueError("sample-start patch handler overlaps cache helper")
    if ENCODER_CACHE_HELPER_START + len(helper) > PATCH_ARM_CAVE_END:
        raise ValueError("sample-start cache helper exceeds secondary ARM cave")
    if len(dsp) > DSP_CAVE_END_ADDRESS - DSP_TRAMPOLINE_ADDRESS:
        raise ValueError("sample-start DSP trampoline exceeds cave")
    hook_start = image_offset(DSP_HOOK_ADDRESS)
    if base[hook_start : hook_start + len(STOCK_DESCRIPTOR_COPY)] != STOCK_DESCRIPTOR_COPY:
        raise ValueError("sample descriptor-copy signature mismatch")

    image = bytearray(base)
    image[image_offset(ARM_CAVE_START) : image_offset(ARM_CAVE_START) + len(arm)] = arm
    image[
        image_offset(PATCH_ARM_CAVE_START) : image_offset(PATCH_ARM_CAVE_START) + len(patch_arm)
    ] = patch_arm
    image[
        image_offset(ENCODER_CACHE_HELPER_START) : image_offset(ENCODER_CACHE_HELPER_START) + len(helper)
    ] = helper
    image[
        image_offset(DSP_TRAMPOLINE_ADDRESS) : image_offset(DSP_TRAMPOLINE_ADDRESS) + len(dsp)
    ] = dsp
    image[hook_start : hook_start + 12] = dsp_bytes(
        [0x0AF080, DSP_TRAMPOLINE_PC, 0x000000, 0x000000]
    )

    hooks = []
    for hook in HOOKS:
        replacement = assemble_thumb(f"bl {ARM_CAVE_START:#x}", hook.address)
        patch_exact(image, hook.address, hook.stock, replacement)
        hooks.append(
            {
                "address": f"{hook.address:#010x}",
                "purpose": hook.purpose,
                "stock": hook.stock.hex(" ").upper(),
                "patched": replacement.hex(" ").upper(),
            }
        )

    changed = {index for index, (before, after) in enumerate(zip(base, image)) if before != after}
    unexpected = sorted(changed - sample_start_allowed_offsets())
    if unexpected:
        raise ValueError(f"sample-start changes escaped declared sites: {unexpected[:16]}")
    if sha256(image) != OUTPUT_IMAGE_SHA256:
        raise ValueError("sample-start output does not match the hardware-tested image")

    return bytes(image), {
        "feature": "per-drum sample-start offset",
        "gesture": "hold Shift and turn Macro 3/4",
        "resolution": "two offset units per encoder event; approximately 64 positions",
        "range": "sample beginning through the final safe sample position",
        "normal_decay": "unchanged and restored while Shift is held",
        "state": "four independent runtime offsets; reset after reboot",
        "trigger_behavior": "new hits only; existing voices are not moved",
        "fade": "no additional fade required in hardware testing",
        "changed_image_bytes": len(changed),
        "arm_control_length": len(arm),
        "arm_patch_length": len(patch_arm),
        "cache_helper_length": len(helper),
        "dsp_trampoline_words": len(dsp) // 3,
        "hooks": hooks,
    }
