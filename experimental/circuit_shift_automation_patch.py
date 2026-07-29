"""Experimental native-lane automation for the Circuit Shift drum controls.

This overlay deliberately starts from the exact hardware-validated v0.4.2
image and reconstructs only the hardware-tested Filter LFO behaviour.  It does
not include the later, unvalidated Clear/reset experiment.

Circuit's seven drum automation lanes store 16 steps * 6 timing points.  Stock
values use 0x00..0x7f and 0xff means empty.  Factory-session analysis found no
0x80..0xfe values, so those values tag absolute hidden Shift states:

    0x80 | sample_start      lane 1 (Decay), 0..126
    0x80 | distortion_type   lane 2 (Distortion), 0..6
    0x80 | filter_lfo_mode   lane 3 (Filter), 0 or 12..15

The stock recorder, timing grid, dirty flags, LED feedback, and erase path are
left in charge.  One hook transforms the value just before the stock recorder
chooses the Drum 1/2 versus Drum 3/4 storage layout.  Playback deliberately
continues through the stock parameter dispatcher; small additions to the three
already-shipped Shift wrappers recognize a tagged value, apply the absolute
hidden state, and restore the normal Macro value.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


ANALYSIS_DIR = Path(__file__).resolve().parent
PUBLISHED_TOOLS = (
    ANALYSIS_DIR.parent
    / "github_publish"
    / "tracelistener-circuit-scale-follow"
    / "tools"
)
sys.path.insert(0, str(ANALYSIS_DIR / "keystone_lib"))
sys.path.insert(0, str(PUBLISHED_TOOLS))
sys.path.insert(0, str(ANALYSIS_DIR))

from keystone import KS_ARCH_ARM, KS_MODE_LITTLE_ENDIAN, KS_MODE_THUMB, Ks

import circuit_filter_lfo_patch as lfo


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80
INPUT_IMAGE_SHA256 = "e6a2b77bea0918e28b2cdadca6bef96654d6a39d7272b3a1f9ac25a51815cca2"

STOCK_DRUM_CONTROL_GETTER = 0x0800D8E4
STOCK_BUTTON_PRESSED = 0x0800C5A8
DSP_SETTER = 0x0801633C
DSP_GETTER = 0x08016E64
PARAMETER_CONTEXT_POINTER = 0x20002DB0
SHIFT_LOGICAL_ID = 0x1B

SAMPLE_RAW_BASE = 0x1E59
SAMPLE_DISPLACEMENT_BASE = 0x1E5D
SAMPLE_DECAY_CACHE_BASE = 0x1E61
DISTORTION_TYPE_BASE = 0x1E65
DISTORTION_AMOUNT_BASE = 0x8101
FILTER_STATE_BASE = 0x1E69
FILTER_AMOUNT_BASE = 0x8105

LANE_SAMPLE_START = 1
LANE_DISTORTION_TYPE = 2
LANE_FILTER_LFO = 3
TAG_BIT = 0x80
EMPTY_AUTOMATION = 0xFF

# Clean Filter-LFO placement.  The first cave contains the four entries, cubic
# centre mapper, then the recorder.  The second contains the shared wrapper.
ARM_LFO_ENTRIES = 0x0803594C
ARM_LFO_FIRST_END = 0x080359F8
ARM_CENTER_MAP = 0x0803595C
ARM_RECORD_HELPER = 0x08035976
ARM_LFO_COMMON = 0x08035B4C
ARM_LFO_SECOND_END = 0x08035BF8

ARM_PARAMETER_READ = 0x0802D494
ARM_PARAMETER_READ_END = 0x0802D4C0
ARM_PARAMETER_RESTORE = 0x0802D4EC
ARM_PARAMETER_RESTORE_END = 0x0802D50C
ARM_NORMAL_CENTER = 0x0802D50C
ARM_NORMAL_CENTER_END = 0x0802D520
ARM_FILTER_TAG_B = 0x0802D5EC
ARM_FILTER_TAG_B_END = 0x0802D600
ARM_FILTER_RESTORE_EXIT = 0x0802D654
ARM_FILTER_NORMAL_EXIT = 0x0802D660
ARM_FILTER_TAG_A = 0x0802D664
ARM_FILTER_TAG_A_END = 0x0802D680

# Tiny fragments left by the verified Sample Start implementation.  Its main
# cave ends with a live literal at $25684, so the query shim uses the 18 bytes
# immediately after the recorder instead of overwriting that literal.
SAMPLE_QUERY_HELPER = 0x080359E6
SAMPLE_QUERY_HELPER_END = ARM_LFO_FIRST_END
SAMPLE_TAG_APPLY = 0x08036816
SAMPLE_TAG_APPLY_END = 0x08036820
SAMPLE_TAG_DECODER = 0x0803683C
SAMPLE_TAG_DECODER_END = 0x0803684D

# Unused padded tail of the verified Distortion wrapper.
# Clear reset, ported from circuit_filter_lfo_patch on 2026-07-28.
# CLEAR_HELD_ID: the logical->physical table at 0x0802EC88 holds 29 entries
# (0..28) but the modifier-ID probe scanned only 0..27 (LOGICAL_BUTTON_COUNT =
# 28), so ID 28 = 0x1C was never tested -- the single unscanned real button.
# It maps to physical 0x07 = scan byte 0 bit 7.  Table decoding is confirmed by
# index 27 = 0x0d = byte 1 bit 5 = Shift.  0x0F was a misread of the probe's
# not-found sentinel (see memory) and is wrong; it boots and never fires.  This layer
# reimplements the LFO application and had never carried the Clear feature at
# all, so Clear was absent rather than broken.  0x0F is the hardware-measured
# held-query ID (modifier-ID probe); the dispatcher's event numbering is a
# different space and latching on it never fired.  The predicate lives in the
# dead DISTORTION_TAG_HELPER block -- that helper is built and reserved but
# never written and nothing branches to it, verified against the built image
# (0 branches, 0 stored pointers into 0x08025D5C..0x08025D90).
CLEAR_HELD_ID = 0x1C
CLEAR_HELPER = 0x08036B08
CLEAR_HELPER_END = 0x08036B28

DISTORTION_TAG_HELPER = 0x08025D5C
DISTORTION_TAG_HELPER_END = 0x08025D90

RECORD_VALUE_HOOK = 0x0801CD6E
RECORD_VALUE_STOCK = bytes.fromhex("02 F1 16 02")  # add.w r2,r2,#0x16

SAMPLE_QUERY_PATCH = 0x080255C6
SAMPLE_QUERY_STOCK = bytes.fromhex("1B 20 E6 F7 EE FF")
SAMPLE_DECODER_PATCH = 0x080255DE
SAMPLE_DECODER_STOCK = bytes.fromhex("01 02 01 D4")

DISTORTION_QUERY_PATCH = 0x08025CE0
DISTORTION_QUERY_STOCK = bytes.fromhex("1B 20 E6 F7 61 FC")

# --- tagged automation playback -------------------------------------------
#
# Tags must be intercepted BEFORE the stock parameter dispatcher, because that
# dispatcher clamps.  The Decay/Distortion/Filter descriptors all bound at
# 0x00..0x7F, so a byte of 0x80..0xFE reaching 0x0800CC58 is clamped to 127 and
# the tag bit is destroyed -- and 127 is then stored as the normal Macro value.
# Detecting the tag downstream in the feature wrappers cannot work: they read
# the value back through STOCK_DRUM_CONTROL_GETTER, which returns the clamped
# byte.
#
# Both playback sites share an identical register setup:
#     r0 = parameter object      r2 = automation byte
#     r3 = parameter-id table    r5 = lane index      r6 = stock setter
# so one decoder serves both.  Replacing `ldrb r1,[r3,r5]; blx r6` with
# `bl decoder` leaves LR pointing at the instruction after the stock call, so
# an untagged value can tail-call the setter with `bx r6` and land exactly
# where `blx r6` would have returned.
PLAYBACK_TICK_PATCH = 0x0800FB02
PLAYBACK_TICK_STOCK = bytes.fromhex("59 5D B0 47")
PLAYBACK_RELOAD_PATCH = 0x08012C4E
PLAYBACK_RELOAD_STOCK = bytes.fromhex("59 5D B0 47")

# Tagged bytes are applied ONLY when the call came from the sequencer tick.
# The pattern-reload site also runs during BOOT session load, before the DSP
# bootstrap has completed; a DSP accessor there waits forever for a DSP that
# is not up yet, which is a hang that presents as a bricked unit.  Hardware
# evidence: a fully acknowledged, error-free transfer (5,981 messages, 9,608
# responses) still failed to boot with tagged bytes present in the stored
# session, while every build without the decoder boots.  The decoder tells
# the two callers apart by LR: the reload site's return address is
# PLAYBACK_RELOAD_PATCH+4 (+1 for Thumb).  Skipped reload tags still bypass
# the stock dispatcher (so they are never clamped into the normal value) and
# get applied by the tick site within one step once playback runs.
# 0x00-filled tail of the DSP-visible program window.  The DSP's reset vector
# jumps to P:$0100 so it never executes here; the shipped distortion wrappers
# already rely on that same property.
PLAYBACK_DECODER = 0x08025E08
PLAYBACK_DECODER_END = 0x08025E74

# Separate slot for the sample-start displacement maths.  It cannot reuse the
# verified path at 0x0802560A: that code falls through into the sample
# wrapper's `pop {r4,r5,r6,r7,r8,pc}`, so entering it from the decoder would
# unwind a frame that was never pushed.
# 0x08032EB0 is NOT free.  Writing 70 bytes there bricks the unit even with
# nothing hooked and nothing ever called -- proven by an isolation image that
# only placed the code.  Zero-filled was never evidence of dead space.
#
# This slot is the tail of the DSP program window, freed when the Filter LFO
# routine shrank from 60 words to 35 during the offset-5 relocation.  It sits
# immediately after that routine, and the neighbouring window region at
# P:$00DC (the decoder) is hardware-proven to boot.
SAMPLE_PLAYBACK_HELPER = 0x08025BE0
SAMPLE_PLAYBACK_HELPER_END = 0x08025C2E

# Parameter ids are base + 7*drum, base 3 = Sample Start (via Decay),
# 4 = Distortion Type, 5 = Filter LFO.
PLAYBACK_FIRST_PARAMETER_ID = 3
PLAYBACK_PARAMETER_STRIDE = 7
SAMPLE_DESCRIPTOR_TABLE = 0x1D0E
DRUM_BLOCK_BASE = 0x9F
DRUM_BLOCK_STRIDE = 10


def sha256(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def image_offset(address: int) -> int:
    result = address - BASE
    if not 0 <= result < IMAGE_SIZE:
        raise ValueError(f"address outside firmware image: {address:#010x}")
    return result


def assemble_thumb(source: str, address: int) -> bytes:
    assembler = Ks(KS_ARCH_ARM, KS_MODE_THUMB | KS_MODE_LITTLE_ENDIAN)
    encoding, _ = assembler.asm(source, address)
    if encoding is None:
        raise ValueError(f"assembler produced no bytes at {address:#010x}")
    return bytes(encoding)


def patch_exact(
    image: bytearray,
    address: int,
    expected: bytes,
    replacement: bytes,
) -> None:
    if len(expected) != len(replacement):
        raise ValueError(f"fixed-size patch changed length at {address:#010x}")
    start = image_offset(address)
    actual = bytes(image[start : start + len(expected)])
    if actual != expected:
        raise ValueError(
            f"signature mismatch at {address:#010x}: expected "
            f"{expected.hex(' ').upper()}, got {actual.hex(' ').upper()}"
        )
    image[start : start + len(replacement)] = replacement


def _check_slot(address: int, code: bytes, end: int, purpose: str) -> None:
    if address + len(code) > end:
        raise ValueError(
            f"{purpose} needs {len(code)} bytes; slot has {end - address}"
        )


# Set False to reproduce the hardware-proven saw LFO exactly (af72cc8d...),
# which steps one mode per encoder event.  The divider improves knob feel but
# was never confirmed to boot, and every failing automation build sat on it.
USE_STEP_DIVIDER = True

# Moved off 0x08032DE8: its neighbour 0x08032EB0 is live memory that bricks
# the unit, so that whole region is untrusted until proven otherwise.
ARM_STEP_DIVIDER = 0x08036A0C
ARM_STEP_DIVIDER_END = 0x08036A34
# Per-drum Shift-encoder event counters, one word each, in the orphaned old
# LFO mode registers (X:$1E69..$1E6C -- ours before the offset-5 relocation,
# still untouched by stock).
STEP_DIVIDER_STATE = 0x1E69
STEP_DIVIDER_EVENTS = 3


def build_step_divider() -> bytes:
    """Gate: pass one Shift-encoder event through per N events.

    The macros are endless encoders, so stepping the LFO mode once per event
    burned through all nine positions in a flick.  This counts events per drum
    and returns r0 = 1 every Nth event, 0 otherwise; the wrapper then applies
    its usual direction logic on the passed event.  Direction reversals within
    a group are not special-cased -- a deliberate simplification to fit the
    slot; at N = 3 the effect is imperceptible.  In: r2 = drum index.
    """

    code = assemble_thumb(
        f"""
            push {{r4, r5, lr}}
            movw r0, #{STEP_DIVIDER_STATE:#x}
            adds r0, r0, r2
            mov r4, r0
            bl {DSP_GETTER:#x}
            lsrs r0, r0, #8
            adds r0, #1
            movs r5, #0
            cmp r0, #{STEP_DIVIDER_EVENTS}
            blo divider_store
            movs r0, #0
            movs r5, #1
        divider_store:
            lsls r0, r0, #8
            mov r1, r4
            bl {DSP_SETTER:#x}
            mov r0, r5
            pop {{r4, r5, pc}}
        """,
        ARM_STEP_DIVIDER,
    )
    _check_slot(ARM_STEP_DIVIDER, code, ARM_STEP_DIVIDER_END, "Shift step gate")
    return code


def build_clean_lfo_helpers() -> list[tuple[int, bytes, int, str]]:
    parameter_read = assemble_thumb(
        f"""
            push {{r2, lr}}
            movs r0, #0x10
            bl {STOCK_DRUM_CONTROL_GETTER:#x}
            mov r7, r0
            ldr r2, [sp]
            movw r0, #{FILTER_AMOUNT_BASE:#x}
            adds r0, r0, r2
            bl {DSP_GETTER:#x}
            asrs r4, r0, #25
            adds r4, #0x40
            pop {{r2, pc}}
        """,
        ARM_PARAMETER_READ,
    )
    parameter_restore = assemble_thumb(
        f"""
            mov r3, r0
            ldr r0, ={PARAMETER_CONTEXT_POINTER:#x}
            ldr r0, [r0]
            ldr r0, [r0, #0x318]
            ldr r2, [r0, #4]
            add.w r2, r2, r1, lsl #3
            ldrh r1, [r2]
            ldr r0, [r0, #0x0c]
            ldr r0, [r0]
            strb r3, [r0, r1]
            bx lr
        """,
        ARM_PARAMETER_RESTORE,
    )
    normal_center = assemble_thumb(
        f"""
            push {{r3, lr}}
            mov r3, r2
            bl {ARM_CENTER_MAP:#x}
            lsls r0, r0, #8
            mov r1, r3
            bl {DSP_SETTER:#x}
            movs r0, #0
            pop {{r3, pc}}
        """,
        ARM_NORMAL_CENTER,
    )
    restore_exit = assemble_thumb(
        f"""
            ldr r1, [sp]
            mov r0, r4
            bl {ARM_PARAMETER_RESTORE:#x}
            mov r0, r4
            pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
        """,
        ARM_FILTER_RESTORE_EXIT,
    )
    normal_exit = assemble_thumb(
        """
            mov r0, r7
            pop {r1, r2, r3, r4, r5, r6, r7, pc}
        """,
        ARM_FILTER_NORMAL_EXIT,
    )
    center_map = assemble_thumb(
        f"""
            mov r2, r0
            mul r0, r0, r2
            mul r0, r0, r2
            lsls r0, r0, #1
            movw r2, #{lfo.CENTER_COEFFICIENT_MIN:#x}
            adds r0, r0, r2
            bic r0, r0, #0x3f
            orrs r0, r1
            bx lr
        """,
        ARM_CENTER_MAP,
    )
    helpers = [
        (ARM_PARAMETER_READ, parameter_read, ARM_PARAMETER_READ_END, "Filter amount read"),
        (
            ARM_PARAMETER_RESTORE,
            parameter_restore,
            ARM_PARAMETER_RESTORE_END,
            "Filter amount restore",
        ),
        (ARM_NORMAL_CENTER, normal_center, ARM_NORMAL_CENTER_END, "Filter centre setter"),
        (
            ARM_FILTER_RESTORE_EXIT,
            restore_exit,
            ARM_FILTER_NORMAL_EXIT,
            "Filter restore exit",
        ),
        (
            ARM_FILTER_NORMAL_EXIT,
            normal_exit,
            ARM_FILTER_TAG_A,
            "Filter normal exit",
        ),
        (ARM_CENTER_MAP, center_map, ARM_RECORD_HELPER, "Filter cubic centre map"),
    ]
    for address, code, end, purpose in helpers:
        _check_slot(address, code, end, purpose)
    if USE_STEP_DIVIDER:
        helpers.append(
            (ARM_STEP_DIVIDER, build_step_divider(), ARM_STEP_DIVIDER_END, "Shift step divider")
        )
    return helpers


def build_filter_tag_helpers() -> list[tuple[int, bytes, int, str]]:
    part_a = assemble_thumb(
        f"""
            ldr r1, [sp]
            ldr r2, [sp, #4]
            bl {ARM_PARAMETER_READ:#x}
            and r0, r7, #0x7f
            cbz r0, filter_tag_valid
            cmp r0, #{lfo.LFO_MODE_MIN}
            blo filter_tag_invalid
            cmp r0, #{lfo.LFO_MODE_MAX}
            bls filter_tag_valid
        filter_tag_invalid:
            movs r0, #0
        filter_tag_valid:
            b.w {ARM_FILTER_TAG_B:#x}
        """,
        ARM_FILTER_TAG_A,
    )
    part_b = assemble_thumb(
        f"""
            bic r1, r5, #0x3f
            orrs r0, r1
            lsls r0, r0, #8
            mov r1, r6
            bl {DSP_SETTER:#x}
            b.w {ARM_FILTER_RESTORE_EXIT:#x}
        """,
        ARM_FILTER_TAG_B,
    )
    helpers = [
        (ARM_FILTER_TAG_A, part_a, ARM_FILTER_TAG_A_END, "Filter tag decode"),
        (ARM_FILTER_TAG_B, part_b, ARM_FILTER_TAG_B_END, "Filter tag apply"),
    ]
    for address, code, end, purpose in helpers:
        _check_slot(address, code, end, purpose)
    return helpers


def build_clean_lfo_arm() -> tuple[list[tuple[int, bytes]], bytes]:
    entries: list[tuple[int, bytes]] = []
    for drum in range(4):
        address = ARM_LFO_ENTRIES + drum * 4
        code = assemble_thumb(
            f"""
                movs r2, #{drum}
                b {ARM_LFO_COMMON:#x}
            """,
            address,
        )
        if len(code) != 4:
            raise ValueError("Filter LFO entry is not four bytes")
        entries.append((address, code))

    if USE_STEP_DIVIDER:
        gate = (
            "ldr r2, [sp, #4]\n"
            f"            bl {ARM_STEP_DIVIDER:#x}\n"
            "            cmp r0, #0\n"
            "            beq restore_amount\n"
            "            "
        )
    else:
        gate = ""

    common = assemble_thumb(
        f"""
            push {{r1, r2, r3, r4, r5, r6, r7, lr}}
            mov r6, r2

            movs r0, #{lfo.LFO_MODE_REGISTER_STRIDE}
            muls r0, r6, r0
            adds r0, #{lfo.LFO_MODE_REGISTERS[0]:#x}
            mov r6, r0
            bl {DSP_GETTER:#x}
            lsrs r5, r0, #8

            movs r0, #{SHIFT_LOGICAL_ID}
            bl {STOCK_BUTTON_PRESSED:#x}
            cbnz r0, shift_event

            ldr r1, [sp]
            movs r0, #0x10
            bl {STOCK_DRUM_CONTROL_GETTER:#x}
            mov r7, r0
            lsls r0, r7, #24
            bmi.w {ARM_FILTER_TAG_A:#x}

            mov r0, r7
            bl {CLEAR_HELPER:#x}

            and r1, r5, #0x3f
            cmp r1, #{lfo.LFO_MODE_MIN}
            blo normal_amount
            cmp r1, #{lfo.LFO_MODE_MAX}
            bhi normal_amount
            mov r0, r7
            mov r2, r6
            bl {ARM_NORMAL_CENTER:#x}
            mov r0, r7
            b wrapper_return

        shift_event:
            ldr r1, [sp]
            ldr r2, [sp, #4]
            bl {ARM_PARAMETER_READ:#x}

            cmp r7, r4
            beq restore_amount
            {gate}cmp r7, r4
            blo step_down

            and r0, r5, #0x3f
            cmp r0, #{lfo.LFO_MODE_MIN}
            bhs step_up_valid
            mov r0, r4
            movs r1, #{lfo.LFO_MODE_MIN}
            bl {ARM_CENTER_MAP:#x}
            b store_state
        step_up_valid:
            cmp r0, #{lfo.LFO_MODE_MAX}
            bhs restore_amount
            adds r0, #1
            bic r1, r5, #0x3f
            orrs r0, r1
            b store_state

        step_down:
            and r0, r5, #0x3f
            cmp r0, #{lfo.LFO_MODE_MIN}
            bls step_off
            cmp r0, #{lfo.LFO_MODE_MAX}
            bhi step_off
            subs r0, #1
            bic r1, r5, #0x3f
            orrs r0, r1
            b store_state
        step_off:
            movs r0, #0

        store_state:
            lsls r0, r0, #8
            mov r1, r6
            bl {DSP_SETTER:#x}
        restore_amount:
            b.w {ARM_FILTER_RESTORE_EXIT:#x}
        normal_amount:
            b.w {ARM_FILTER_NORMAL_EXIT:#x}
        wrapper_return:
            pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
        """,
        ARM_LFO_COMMON,
    )
    _check_slot(
        ARM_LFO_COMMON,
        common,
        ARM_LFO_SECOND_END,
        "clean Filter LFO wrapper with tag playback",
    )
    return entries, common


def build_sample_tag_helpers() -> list[tuple[int, bytes, int, str]]:
    # Replace the physical-Shift query with a tiny shim.  A tagged automation
    # byte forces the existing Shift path; normal values execute the original
    # held-button query and return to its stock comparison.
    # Both arms share a single wide exit branch.  Two separate `b` exits assemble
    # to 4 bytes each and made this 20 bytes, but the slot between the record
    # helper and the end of the first LFO cave is exactly 18.  Folding them costs
    # one 2-byte short branch and saves 2, which is the difference between
    # fitting and not.  It cannot move to 0x08025684: that address holds the live
    # literal 0x20002DB0 (the parameter context pointer), read by the `ldr r0,
    # [pc, #0x30]` at 0x08025650.
    query = assemble_thumb(
        f"""
            lsls r1, r5, #24
            bmi sample_force_shift
            movs r0, #{SHIFT_LOGICAL_ID}
            bl {STOCK_BUTTON_PRESSED:#x}
            b sample_query_exit
        sample_force_shift:
            movs r0, #1
        sample_query_exit:
            b {SAMPLE_QUERY_PATCH + 6:#x}
        """,
        SAMPLE_QUERY_HELPER,
    )
    # Entered after the verified wrapper has loaded the normal Decay into r7.
    # For a tag, strip the tag then jump directly to its absolute-value apply
    # path.  Otherwise reproduce the two overwritten cache-validity branches.
    decoder = assemble_thumb(
        f"""
            lsls r1, r5, #24
            bmi sample_tag
            lsls r1, r0, #8
            bmi.w 0x080255e6
            b.w 0x080255e2
        sample_tag:
            b {SAMPLE_TAG_APPLY:#x}
        """,
        SAMPLE_TAG_DECODER,
    )
    apply = assemble_thumb(
        """
            lsls r5, r5, #25
            lsrs r5, r5, #25
            b.w 0x0802560a
        """,
        SAMPLE_TAG_APPLY,
    )
    helpers = [
        (SAMPLE_QUERY_HELPER, query, SAMPLE_QUERY_HELPER_END, "Sample tag Shift shim"),
        (
            SAMPLE_TAG_DECODER,
            decoder,
            SAMPLE_TAG_DECODER_END,
            "Sample tag decoder",
        ),
        (SAMPLE_TAG_APPLY, apply, SAMPLE_TAG_APPLY_END, "Sample absolute apply entry"),
    ]
    for address, code, end, purpose in helpers:
        _check_slot(address, code, end, purpose)
    return helpers


def build_clear_helper() -> bytes:
    """Hold Clear and move Macro 7/8 -> switch this drum's LFO off.

    The value gate ("macro sits at its factory default") was ported from the
    LFO module and is WRONG for this gesture: the Circuit's documented Clear
    action is hold-Clear-and-move-the-control, so during the real gesture the
    macro reads wherever it was moved to, never 64, and the gate never matched.
    Firing on a live Clear read alone is the correct gesture.

    Called with r0 = live macro value, r6 = this drum's mode register, r7 = the
    same live value.  On a hit it drops its own saved lr and pops the wrapper's
    frame directly, returning to the wrapper's caller with r0 = r7 -- that is
    what keeps the call site down to the 6 bytes the cave had left.  r4-r7 are
    callee-saved, so r6/r7 survive both calls below.
    """
    code = assemble_thumb(
        f"""
            push {{lr}}
            movs r0, #{CLEAR_HELD_ID:#x}
            bl {STOCK_BUTTON_PRESSED:#x}
            cbz r0, clear_no
            movs r0, #0
            mov r1, r6
            bl {DSP_SETTER:#x}
            add sp, #4
            mov r0, r7
            pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
        clear_no:
            pop {{pc}}
        """,
        CLEAR_HELPER,
    )
    _check_slot(CLEAR_HELPER, code, CLEAR_HELPER_END, "Clear helper")
    return code


def build_distortion_tag_helper() -> bytes:
    code = assemble_thumb(
        f"""
            lsls r0, r7, #24
            bmi distortion_tag
            movs r0, #{SHIFT_LOGICAL_ID}
            bl {STOCK_BUTTON_PRESSED:#x}
            b {DISTORTION_QUERY_PATCH + 6:#x}

        distortion_tag:
            movw r0, #{DISTORTION_AMOUNT_BASE:#x}
            adds r0, r0, r6
            bl {DSP_GETTER:#x}
            lsrs r4, r0, #24
            and r7, r7, #0x7f
            cmp r7, #6
            bls distortion_tag_valid
            movs r7, #0
        distortion_tag_valid:
            movw r0, #{DISTORTION_TYPE_BASE:#x}
            adds r6, r0, r6
            movs r2, #1
            b.w 0x08025d28
        """,
        DISTORTION_TAG_HELPER,
    )
    _check_slot(
        DISTORTION_TAG_HELPER,
        code,
        DISTORTION_TAG_HELPER_END,
        "Distortion tag helper",
    )
    return code


def build_record_helper() -> bytes:
    """Encode the current hidden state as a tagged byte before the stock store.

    Corrected after hardware bricked twice.  Two faults in the previous version:

    1. It saved the mode selector r2 into r4 and then reused r4 as the drum
       index, so every DSP read was at base+mode instead of base+drum.  There is
       no drum number in any register at this hook, which is why that stood in
       for one.
    2. The drum index it did compute lived in a caller-saved register, which
       `bl DSP_GETTER` destroys.

    The parameter id is available: on the path reaching this hook r1 equals
    [object+0x50], the lane->parameter-id byte map that both playback sites read
    with `ldrb r1,[r3,r5]`.  So `ldrb [r1, r5]` yields the id, and drum and
    feature follow from id-3 divided by seven -- the same decode the playback
    decoder uses.  The drum index is kept in r7, which is callee-saved and
    therefore survives the DSP calls.

    CRITICAL: r1 (the parameter id) must be captured BEFORE the
    STOCK_BUTTON_PRESSED call, which clobbers r0-r3.  Reading it afterwards
    decoded garbage; when that garbage happened to land in 3..26 the helper
    resolved a bogus drum, read a bogus DSP register and produced a wild r6,
    which the caller feeds into `mla r0, r0, r6, r3` -- a wild write.  That is
    why builds with this helper booted intermittently rather than never.
    r5 is used for the capture because it is callee-saved and survives the call.

    r6 carries the value the stock code is about to store and is deliberately
    rewritten, but only once a tag has actually been produced.  Every early exit
    leaves r6 untouched so normal recording is bit-for-bit stock.  This matters:
    the enclosing function also uses r6 in address arithmetic
    (`mla r0, r0, r6, r3` at 0x0801CD34), so a bogus r6 becomes a wild write.

    The displaced `add.w r2,r2,#0x16` is reproduced at the end, and `cmp r2,
    #0x17` rebuilds the flags that `bhi` at 0x0801CD76 consumes from the `cmp
    r2,#1` at 0x0801CD6C: comparing r2+0x16 against 1+0x16 is equivalent for the
    unsigned condition.
    """

    code = assemble_thumb(
        f"""
            push {{r0, r1, r3, r4, r5, r7, lr}}
            mov r4, r2
            mov r5, r1

            movs r0, #{SHIFT_LOGICAL_ID}
            bl {STOCK_BUTTON_PRESSED:#x}
            cbz r0, record_done

            mov r0, r5
            subs r0, #{PLAYBACK_FIRST_PARAMETER_ID}
            blo record_done
            cmp r0, #24
            bhs record_done
            movs r7, #0
        record_scan:
            cmp r0, #{PLAYBACK_PARAMETER_STRIDE}
            blo record_have
            subs r0, #{PLAYBACK_PARAMETER_STRIDE}
            adds r7, #1
            b record_scan
        record_have:
            cmp r0, #1
            beq record_distortion
            cmp r0, #2
            beq record_filter
            cbnz r0, record_done

            movw r0, #{SAMPLE_RAW_BASE:#x}
            adds r0, r0, r7
            bl {DSP_GETTER:#x}
            lsrs r6, r0, #24
            cmp r6, #126
            bls record_tag
            movs r6, #126
            b record_tag

        record_distortion:
            movw r0, #{DISTORTION_TYPE_BASE:#x}
            adds r0, r0, r7
            bl {DSP_GETTER:#x}
            lsrs r6, r0, #8
            cmp r6, #6
            bls record_tag
            movs r6, #0
            b record_tag

        record_filter:
            movs r0, #{lfo.LFO_MODE_REGISTER_STRIDE}
            muls r0, r7, r0
            adds r0, #{lfo.LFO_MODE_REGISTERS[0]:#x}
            bl {DSP_GETTER:#x}
            lsrs r6, r0, #8
            and r6, r6, #0x3f
            cbz r6, record_tag
            cmp r6, #{lfo.LFO_MODE_MIN}
            blo record_filter_off
            cmp r6, #{lfo.LFO_MODE_MAX}
            bls record_tag
        record_filter_off:
            movs r6, #0

        record_tag:
            orr r6, r6, #{TAG_BIT:#x}
        record_done:
            mov r2, r4
            pop {{r0, r1, r3, r4, r5, r7, lr}}
            adds r2, #0x16
            cmp r2, #0x17
            bx lr
        """,
        ARM_RECORD_HELPER,
    )
    _check_slot(
        ARM_RECORD_HELPER, code, ARM_LFO_FIRST_END, "Shift automation recorder"
    )
    return code


FILTER_APPLY_HELPER = 0x08036B2C
FILTER_APPLY_HELPER_END = 0x08036B68


def build_filter_apply_helper() -> bytes:
    """Apply a tagged Filter LFO mode: stride-10 register, centre default.

    In: r0 = mode value (0..63, the DSP validates), r1 = drum index.  Reads the
    packed word at X:$00A4 + 10*drum, keeps the centre bits, substitutes the
    ~596 Hz default when no centre was ever set (a replayed mode times a zero
    centre is silent -- the "nudge Macro 7 first" symptom), writes back with
    the recorded mode.  Runs only for tagged bytes, never on the stock path.
    """

    code = assemble_thumb(
        f"""
            push {{r4, r5, lr}}
            mov r5, r0
            movs r4, #{lfo.LFO_MODE_REGISTER_STRIDE}
            muls r4, r1, r4
            adds r4, #{lfo.LFO_MODE_REGISTERS[0]:#x}
            mov r0, r4
            bl {DSP_GETTER:#x}
            lsrs r0, r0, #8
            bics r0, r0, #0x3f
            bne filter_apply_centre_ok
            movs r0, #0x0a
            lsls r0, r0, #16
        filter_apply_centre_ok:
            orrs r0, r5
            lsls r0, r0, #8
            mov r1, r4
            bl {DSP_SETTER:#x}
            pop {{r4, r5, pc}}
        """,
        FILTER_APPLY_HELPER,
    )
    _check_slot(FILTER_APPLY_HELPER, code, FILTER_APPLY_HELPER_END, "filter apply helper")
    return code


def build_sample_playback_helper() -> bytes:
    """Apply a tagged Sample Start value: raw word plus proportional displacement.

    Mirrors the verified arithmetic at 0x0802560A..0x0802564C, which reads the
    drum's sample id from parameter-block offset 0, indexes the descriptor table
    at X:$1D0E, and scales the length by the 0..127 position.  Entered with
    r0 = value (0..127) and r1 = drum index.

    The verified path also calls 0x08036820 to refresh the decay/encoder cache at
    X:$1E61+drum, which is omitted here because that helper needs r7/r8/r6 from
    inside the sample wrapper.  The wrapper validates that cache before use, so a
    stale entry degrades rather than misbehaves.
    """

    code = assemble_thumb(
        f"""
            push {{r4, r5, lr}}
            mov r4, r0
            mov r5, r1

            lsls r0, r4, #24
            movw r1, #{SAMPLE_RAW_BASE:#x}
            adds r1, r1, r5
            bl {DSP_SETTER:#x}

            movs r1, #{DRUM_BLOCK_STRIDE}
            muls r1, r5, r1
            adds r1, #{DRUM_BLOCK_BASE:#x}
            mov r0, r1
            bl {DSP_GETTER:#x}
            lsrs r0, r0, #8
            lsls r0, r0, #2
            movw r1, #{SAMPLE_DESCRIPTOR_TABLE:#x}
            adds r1, r1, r0
            mov r0, r1
            bl {DSP_GETTER:#x}

            cmp r4, #0x7f
            beq sample_playback_end_stop
            lsrs r0, r0, #7
            muls r0, r4, r0
            b sample_playback_store
        sample_playback_end_stop:
            subs r0, r0, #1
        sample_playback_store:
            movw r1, #{SAMPLE_DISPLACEMENT_BASE:#x}
            adds r1, r1, r5
            bl {DSP_SETTER:#x}
            pop {{r4, r5, pc}}
        """,
        SAMPLE_PLAYBACK_HELPER,
    )
    _check_slot(
        SAMPLE_PLAYBACK_HELPER,
        code,
        SAMPLE_PLAYBACK_HELPER_END,
        "Sample Start playback helper",
    )
    return code


def build_playback_decoder() -> bytes:
    """Shared decoder for both automation playback sites.

    Untagged bytes reproduce the two displaced instructions exactly.  Tagged
    bytes apply the hidden state and return without ever reaching the stock
    parameter dispatcher, so the stored normal Macro value is untouched.

    r4 is live at the tick site (it supplies both r0 and r3) so it is saved and
    restored; r5 and r6 are never touched.
    """

    code = assemble_thumb(
        f"""
            ldrb r1, [r3, r5]
            lsls r3, r2, #24
            bmi playback_tagged
            bx r6

        playback_tagged:
            push {{r4, lr}}
            mov r4, lr
            movw r3, #{(PLAYBACK_RELOAD_PATCH + 5) & 0xFFFF:#x}
            movt r3, #{(PLAYBACK_RELOAD_PATCH + 5) >> 16:#x}
            cmp r4, r3
            beq playback_done
            subs r1, #{PLAYBACK_FIRST_PARAMETER_ID}
            blo playback_done
            movs r4, #0
        playback_scan:
            cmp r1, #{PLAYBACK_PARAMETER_STRIDE}
            blo playback_have
            subs r1, #{PLAYBACK_PARAMETER_STRIDE}
            adds r4, #1
            cmp r4, #4
            blo playback_scan
            b playback_done
        playback_have:
            cmp r1, #2
            bhi playback_done
            and r2, r2, #0x7f

            cmp r1, #1
            beq playback_distortion
            cmp r1, #2
            beq playback_filter

            mov r0, r2
            mov r1, r4
            bl {SAMPLE_PLAYBACK_HELPER:#x}
            b playback_done

        playback_distortion:
            lsls r0, r2, #8
            movw r1, #{DISTORTION_TYPE_BASE:#x}
            adds r1, r1, r4
            bl {DSP_SETTER:#x}
            b playback_done

        playback_filter:
            mov r0, r2
            mov r1, r4
            bl {FILTER_APPLY_HELPER:#x}

        playback_done:
            pop {{r4, pc}}
        """,
        PLAYBACK_DECODER,
    )
    _check_slot(
        PLAYBACK_DECODER, code, PLAYBACK_DECODER_END, "tagged automation playback decoder"
    )
    return code


def clean_lfo_allowed_offsets() -> set[int]:
    dsp_words, _labels = lfo.build_dsp_lfo_words()
    entries, common = build_clean_lfo_arm()
    blocks = list(build_clean_lfo_helpers()) + list(build_filter_tag_helpers())
    allowed = set(
        range(
            image_offset(lfo.DSP_LFO_ADDRESS),
            image_offset(lfo.DSP_LFO_ADDRESS) + len(lfo.dsp_bytes(dsp_words)),
        )
    )
    allowed.update(
        range(image_offset(lfo.DSP_HOOK_ADDRESS), image_offset(lfo.DSP_HOOK_ADDRESS) + 6)
    )
    for address, code in entries:
        allowed.update(range(image_offset(address), image_offset(address) + len(code)))
    allowed.update(
        range(image_offset(ARM_LFO_COMMON), image_offset(ARM_LFO_COMMON) + len(common))
    )
    for address, code, _end, _purpose in blocks:
        allowed.update(range(image_offset(address), image_offset(address) + len(code)))
    for hook in lfo.FILTER_HOOKS:
        allowed.update(range(image_offset(hook.address), image_offset(hook.address) + 4))
    helper = build_clear_helper()
    allowed.update(
        range(image_offset(CLEAR_HELPER), image_offset(CLEAR_HELPER) + len(helper))
    )
    return allowed


def apply_clean_filter_lfo(base_image: bytes) -> tuple[bytes, dict]:
    if len(base_image) != IMAGE_SIZE or sha256(base_image) != INPUT_IMAGE_SHA256:
        raise ValueError("input is not the exact hardware-validated v0.4.2 image")
    image = bytearray(base_image)
    dsp_words, dsp_labels = lfo.build_dsp_lfo_words()
    dsp_code = lfo.dsp_bytes(dsp_words)
    entries, common = build_clean_lfo_arm()
    helpers = list(build_clean_lfo_helpers()) + list(build_filter_tag_helpers())

    if any(
        image[
            image_offset(lfo.DSP_LFO_ADDRESS) :
            image_offset(lfo.DSP_LFO_ADDRESS) + len(dsp_code)
        ]
    ):
        raise ValueError("DSP Filter LFO cave is not zero")
    for start, end in (
        (ARM_LFO_ENTRIES, ARM_LFO_FIRST_END),
        (ARM_LFO_COMMON, ARM_LFO_SECOND_END),
    ):
        if any(value != 0xFF for value in image[image_offset(start) : image_offset(end)]):
            raise ValueError(f"ARM Filter LFO cave {start:#010x} is not 0xFF")

    image[
        image_offset(lfo.DSP_LFO_ADDRESS) :
        image_offset(lfo.DSP_LFO_ADDRESS) + len(dsp_code)
    ] = dsp_code
    for address, code in entries:
        image[image_offset(address) : image_offset(address) + len(code)] = code
    for address, code, _end, purpose in helpers:
        region = image[image_offset(address) : image_offset(address) + len(code)]
        expected = 0xFF if ARM_LFO_ENTRIES <= address < ARM_LFO_FIRST_END else 0
        if any(value != expected for value in region):
            raise ValueError(f"{purpose} slot is not available")
        image[image_offset(address) : image_offset(address) + len(code)] = code
    image[
        image_offset(ARM_LFO_COMMON) : image_offset(ARM_LFO_COMMON) + len(common)
    ] = common

    helper = build_clear_helper()
    region = image[
        image_offset(CLEAR_HELPER) : image_offset(CLEAR_HELPER) + len(helper)
    ]
    if any(region):
        raise ValueError("Clear helper slot is not free")
    image[
        image_offset(CLEAR_HELPER) : image_offset(CLEAR_HELPER) + len(helper)
    ] = helper

    patch_exact(
        image,
        lfo.DSP_HOOK_ADDRESS,
        lfo.DSP_STOCK_COEFFICIENT_LOAD,
        lfo.dsp_bytes([0x0BF080, lfo.DSP_LFO_PC]),
    )
    for index, hook in enumerate(lfo.FILTER_HOOKS):
        patch_exact(
            image,
            hook.address,
            hook.stock,
            assemble_thumb(f"bl {ARM_LFO_ENTRIES + index * 4:#x}", hook.address),
        )

    changed = {
        index
        for index, (before, after) in enumerate(zip(base_image, image))
        if before != after
    }
    unexpected = sorted(changed - clean_lfo_allowed_offsets())
    if unexpected:
        raise ValueError(f"clean Filter LFO changed undeclared offsets: {unexpected[:16]}")
    return bytes(image), {
        "feature": "hardware-good Filter LFO reconstruction plus tag playback",
        "dsp_words": len(dsp_words),
        "dsp_labels": dsp_labels,
        "arm_common_bytes": len(common),
    }


def automation_allowed_offsets() -> set[int]:
    allowed = set()
    distortion = build_distortion_tag_helper()
    allowed.update(
        range(
            image_offset(DISTORTION_TAG_HELPER),
            image_offset(DISTORTION_TAG_HELPER) + len(distortion),
        )
    )
    recorder = build_record_helper()
    allowed.update(
        range(image_offset(ARM_RECORD_HELPER), image_offset(ARM_RECORD_HELPER) + len(recorder))
    )
    for address, length in (
        (RECORD_VALUE_HOOK, 4),
        (PLAYBACK_TICK_PATCH, 4),
        (PLAYBACK_RELOAD_PATCH, 4),
    ):
        allowed.update(range(image_offset(address), image_offset(address) + length))
    decoder = build_playback_decoder()
    allowed.update(
        range(image_offset(PLAYBACK_DECODER), image_offset(PLAYBACK_DECODER) + len(decoder))
    )
    sample_helper = build_sample_playback_helper()
    allowed.update(
        range(
            image_offset(SAMPLE_PLAYBACK_HELPER),
            image_offset(SAMPLE_PLAYBACK_HELPER) + len(sample_helper),
        )
    )
    filter_helper = build_filter_apply_helper()
    allowed.update(
        range(
            image_offset(FILTER_APPLY_HELPER),
            image_offset(FILTER_APPLY_HELPER) + len(filter_helper),
        )
    )
    return allowed


def apply_shift_automation(clean_lfo_image: bytes) -> tuple[bytes, dict]:
    """Add tagged Shift automation: recorder plus the shared playback decoder.

    The Sample/Distortion query shims that earlier versions spliced into the two
    feature wrappers are gone.  They implemented the abandoned design in which a
    tagged byte travelled through the stock parameter dispatcher and each wrapper
    sniffed bit 7, which cannot work because that dispatcher clamps to 0..127 and
    destroys the tag.  The playback decoder replaced them by intercepting before
    the dispatcher, and on hardware the shims were also what crashed the unit on
    Shift + Macro 3/4 and 5/6.  Their slot is now needed by the larger recorder.
    """

    image = bytearray(clean_lfo_image)
    record_helper = build_record_helper()

    record_region = image[
        image_offset(ARM_RECORD_HELPER) :
        image_offset(ARM_RECORD_HELPER) + len(record_helper)
    ]
    if any(value != 0xFF for value in record_region):
        raise ValueError("record helper slot is not 0xFF")
    image[
        image_offset(ARM_RECORD_HELPER) :
        image_offset(ARM_RECORD_HELPER) + len(record_helper)
    ] = record_helper

    patch_exact(
        image,
        RECORD_VALUE_HOOK,
        RECORD_VALUE_STOCK,
        assemble_thumb(f"bl {ARM_RECORD_HELPER:#x}", RECORD_VALUE_HOOK),
    )

    decoder = build_playback_decoder()
    sample_helper = build_sample_playback_helper()
    filter_helper = build_filter_apply_helper()
    for address, code, purpose in (
        (PLAYBACK_DECODER, decoder, "playback decoder"),
        (SAMPLE_PLAYBACK_HELPER, sample_helper, "sample playback helper"),
        (FILTER_APPLY_HELPER, filter_helper, "filter apply helper"),
    ):
        region = image[image_offset(address) : image_offset(address) + len(code)]
        if any(value != 0 for value in region):
            raise ValueError(f"{purpose} slot is not zero-filled")
        image[image_offset(address) : image_offset(address) + len(code)] = code

    for address, stock in (
        (PLAYBACK_TICK_PATCH, PLAYBACK_TICK_STOCK),
        (PLAYBACK_RELOAD_PATCH, PLAYBACK_RELOAD_STOCK),
    ):
        patch_exact(
            image,
            address,
            stock,
            assemble_thumb(f"bl {PLAYBACK_DECODER:#x}", address),
        )

    changed = {
        index
        for index, (before, after) in enumerate(zip(clean_lfo_image, image))
        if before != after
    }
    unexpected = sorted(changed - automation_allowed_offsets())
    if unexpected:
        raise ValueError(f"Shift automation changed undeclared offsets: {unexpected[:16]}")
    return bytes(image), {
        "feature": "native tagged Shift automation",
        "tag_range": ["0x80", "0xFE"],
        "empty": "0xFF",
        "lanes": {
            "1": "Sample Start 0..126",
            "2": "Distortion Type 0..6",
            "3": "Filter LFO Off or 12..15",
        },
        "record_helper_bytes": len(record_helper),
        "filter_wrapper_bytes": len(build_clean_lfo_arm()[1]),
    }


def apply_all(base_image: bytes) -> tuple[bytes, dict]:
    clean, lfo_manifest = apply_clean_filter_lfo(base_image)
    patched, automation_manifest = apply_shift_automation(clean)
    return patched, {
        "base_image_sha256": sha256(base_image),
        "clean_lfo_image_sha256": sha256(clean),
        "patched_image_sha256": sha256(patched),
        "filter_lfo": lfo_manifest,
        "shift_automation": automation_manifest,
    }
