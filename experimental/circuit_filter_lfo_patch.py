"""Independent per-drum triangle filter LFO for Circuit firmware 3592.

This probe is stacked on the exact hardware-validated v0.4.2 image.  It keeps
the stock bipolar Drum Filter amount intact and changes only the one-pole
coefficient used by the stock filter:

* normal Macro 7/8 remains low-pass -> bypass -> high-pass;
* Shift + Macro 7/8 selects Off or one of four triangle speeds;
* four mode words live at X:$1E69..$1E6C, outside session and synth state;
* the DSP validates every mode on every block, so reset garbage means Off.

The triangle is derived from the stock audio-block counter X:$0065.  No synth
LFO state, patch state, tempo state, or per-drum phase accumulator is reused.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


ANALYSIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ANALYSIS_DIR / "keystone_lib"))

from keystone import KS_ARCH_ARM, KS_MODE_LITTLE_ENDIAN, KS_MODE_THUMB, Ks


BASE = 0x08008000
IMAGE_SIZE = 0x2EB80
INPUT_IMAGE_SHA256 = "e6a2b77bea0918e28b2cdadca6bef96654d6a39d7272b3a1f9ac25a51815cca2"

STOCK_DRUM_CONTROL_GETTER = 0x0800D8E4
STOCK_BUTTON_PRESSED = 0x0800C5A8
DSP_SETTER = 0x0801633C
DSP_GETTER = 0x08016E64
PARAMETER_CONTEXT_POINTER = 0x20002DB0
SHIFT_LOGICAL_ID = 0x1B
# Clear is panel event 0x19.  It must not be passed to
# STOCK_BUTTON_PRESSED: that boot-combination helper maps 0x19 to a scan line
# which reads high at rest on this hardware.  The already-shipped Scale Follow
# panel-event hook sees the real UI event instead.  It arms one spare state bit,
# and the next stock Macro reset consumes it only when the callback value equals
# that Macro's factory default.  This preserves the stock reset and blue LED.
# Clear = logical ID 0x0F, identified on hardware with the modifier-ID probe
# rather than inferred.  Held with Scales, the probe reported drums 2/3/4 stuck
# (bits 1,2,3 = 14) and drum 1 stuck high (bit 0); bits 4,0 = 10 or 11 would
# encode 30 or 31, outside the 0..27 scan range, so 15 is the only consistent
# answer.  0x0F maps to physical 61 = scan byte 7 bit 5, the same column as
# Shift (0x1B, byte 1 bit 5).
#
# Do NOT use 0x19: it reads permanently held on this hardware, which silently
# took every Shift+Macro gesture down the reset path.  0x1A is not Clear either
# -- it reads false even when Clear is pressed.  Both were guesses from the
# dispatcher table; this one is measured.
CLEAR_EVENT_ID = 0xFF  # latch retired; the predicate polls CLEAR_HELD_ID live
# The dispatcher's event numbering is NOT the held-query numbering -- arming the
# latch on event 0x0F never fired, which is why Clear did nothing.  The probe
# found 0x0F *through* STOCK_BUTTON_PRESSED, so that is the call that is proven
# to see this button on this hardware; poll it instead of latching.
CLEAR_HELD_ID = 0x0F
CLEAR_ARMED_BIT = 0x10

# These are the only two remaining substantial 0xFF runs in v0.4.2.  The
# first holds four entries plus the shared reset/centre helpers; the second
# holds the Filter wrapper.
ARM_CAVE_START = 0x0803594C
ARM_FIRST_CAVE_END = 0x080359F8
ARM_SCALE_RESET_START = ARM_CAVE_START + 0x10
ARM_SCALE_RESET_END = 0x0803598C
ARM_SAMPLE_RESET_START = ARM_SCALE_RESET_END
ARM_SAMPLE_RESET_END = 0x080359D0
ARM_CENTER_MAP_START = ARM_SAMPLE_RESET_END
ARM_CENTER_MAP_END = ARM_FIRST_CAVE_END
ARM_COMMON_START = 0x08035B4C
ARM_CAVE_END = 0x08035BF8
ARM_CAVE_FILL = 0xFF
ARM_PARAMETER_READ_START = 0x0802D494
ARM_PARAMETER_READ_END = 0x0802D4C0
ARM_PARAMETER_RESTORE_START = 0x0802D4EC
ARM_PARAMETER_RESTORE_END = 0x0802D50C
ARM_NORMAL_CENTER_START = ARM_PARAMETER_RESTORE_END
ARM_NORMAL_CENTER_END = 0x0802D520
ARM_FILTER_RESTORE_EXIT = 0x0802D654
ARM_FILTER_NORMAL_EXIT = 0x0802D660
ARM_FILTER_CLEAR_EXIT = 0x0802D66A
ARM_FILTER_EXITS_END = 0x0802D680
ARM_PANEL_EVENT_HOOK_START = 0x0802D610
ARM_PANEL_EVENT_HOOK_END = 0x0802D654
ARM_REFRESH_ALL_DRUM_PITCH = 0x0802D600
ARM_MAIN_HANDLER_RESUME = 0x0801DE00

BASE_PANEL_EVENT_HOOK = bytes.fromhex(
    "F8 B5 07 B4 0E 4A 10 78 10 F0 20 0F 06 D1 40 F0 "
    "A0 00 10 70 0B 46 FF F7 EB FF 19 46 1B 29 06 D0 "
    "12 29 08 D0 10 78 20 F0 40 00 10 70 03 E0 10 78 "
    "40 F0 40 00 10 70 07 BC 04 46 F0 F7 D9 BB 00 BF "
    "AE 2D 00 20"
)

DSP_PROGRAM_DATA = 0x08025B6E + 6
DSP_HOOK_PC = 0x2773
DSP_HOOK_ADDRESS = DSP_PROGRAM_DATA + DSP_HOOK_PC * 3
DSP_STOCK_COEFFICIENT_LOAD = bytes.fromhex("47 F4 00 09 99 9A")
DSP_LFO_PC = 0x0001
DSP_LFO_ADDRESS = DSP_PROGRAM_DATA + DSP_LFO_PC * 3
DSP_LFO_END_PC = 0x003E
DSP_LFO_END_ADDRESS = DSP_PROGRAM_DATA + DSP_LFO_END_PC * 3
DSP_STOCK_CAVE_WORD = 0x000000

STOCK_COEFFICIENT = 0x09999A
CENTER_COEFFICIENT_MIN = 0x008000
CENTER_COEFFICIENT_MAX = 0x3F02C0
# The triangle sweeps the centre over 0.0 .. 2.0, so the reachable coefficient
# runs from fully closed up to twice the maximum centre.  0x3F02C0 * 2 =
# 0x7E0580, still below 1.0 (0x800000), so the accumulator-to-data move that
# writes the coefficient cannot saturate.
LFO_COEFFICIENT_MIN = 0x000000
LFO_COEFFICIENT_MAX = 0x7E0580

# Stored values are the variable left-shift count applied to the 24-bit block
# counter.  With 48 kHz / 32-sample blocks these produce approximately:
# 12 -> 0.366 Hz, 13 -> 0.732 Hz, 14 -> 1.465 Hz, 15 -> 2.930 Hz.
LFO_MODE_MIN = 12
LFO_MODE_MAX = 19  # 12..15 triangle, 16..19 sawtooth
# Packed centre|mode now lives at drum parameter-block offset 5
# (X:$00A4/$00AE/$00B8/$00C2), proven dead three ways during the cutoff-probe
# work: the voice reads offsets 0,1,2,3,4,6,7,8,9; stock ARM writes 0,1,2,4,8,9;
# the DSP smoothers at P:$21BC/$218E write 3,4 and 6,7.  r2 already points at
# the drum's block, so the DSP routine reads its own drum's word with a single
# `move x:(r2+<$5),b` and the old 27-word drum-select chain is gone.
LFO_MODE_REGISTERS = tuple(0x00A4 + 10 * index for index in range(4))
LFO_MODE_REGISTER_STRIDE = 10
FILTER_AMOUNT_REGISTER_BASE = 0x8105

# Existing v0.4.2 feature locations used by the unified reset overlay.
STATE_SCALE_MODE = 0x20002DAE
SCALE_MODE_BIT = 0x80
SCALE_INITIALIZED_BIT = 0x20
MAPPED_PITCH_GETTER = 0x0802D520
SCALE_LIVE_HOOKS = (
    (0x08018B10, bytes.fromhex("14 F0 06 FD")),
    (0x0801A16A, bytes.fromhex("13 F0 D9 F9")),
    (0x08019F0A, bytes.fromhex("13 F0 09 FB")),
    (0x08019E72, bytes.fromhex("13 F0 55 FB")),
)

SAMPLE_MODIFIER_BRANCH = 0x080255CC
SAMPLE_MODIFIER_STOCK = bytes.fromhex("00 28 4C D0")
SAMPLE_SHIFT_PATH = 0x080255D0
SAMPLE_NORMAL_DECAY = 0x0802566A
SAMPLE_OFFSET_RAW_BASE = 0x1E59
SAMPLE_OFFSET_DISPLACEMENT_BASE = 0x1E5D
SAMPLE_DECAY_CACHE_BASE = 0x1E61

DISTORTION_ARM_COMMON = 0x08025CD4
DISTORTION_CLEAR_PREDICATE = 0x08025D70
DISTORTION_ARM_SLOT_END = 0x08025D90
DISTORTION_TYPE_BASE = 0x1E65


@dataclass(frozen=True)
class FilterHook:
    address: int
    stock: bytes
    drum: int
    parameter_id: int


FILTER_HOOKS = (
    FilterHook(0x08018B58, bytes.fromhex("F4 F7 C4 FE"), 1, 0x05),
    FilterHook(0x0801A1B2, bytes.fromhex("F3 F7 97 FB"), 2, 0x0C),
    FilterHook(0x08019F52, bytes.fromhex("F3 F7 C7 FC"), 3, 0x13),
    FilterHook(0x08019EBA, bytes.fromhex("F3 F7 13 FD"), 4, 0x1A),
)


def image_offset(address: int) -> int:
    result = address - BASE
    if not 0 <= result < IMAGE_SIZE:
        raise ValueError(f"address outside firmware image: {address:#010x}")
    return result


def sha256(data: bytes | bytearray) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


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


def patch_bytes(
    image: bytearray,
    address: int,
    expected: bytes,
    replacement: bytes,
) -> None:
    if len(expected) != len(replacement):
        raise ValueError("fixed-size patch changed length")
    start = image_offset(address)
    actual = bytes(image[start : start + len(expected)])
    if actual != expected:
        raise ValueError(
            f"signature mismatch at {address:#010x}: expected "
            f"{expected.hex(' ').upper()}, got {actual.hex(' ').upper()}"
        )
    image[start : start + len(replacement)] = replacement


def build_dsp_lfo_words() -> tuple[list[int], dict[str, int]]:
    """Per-drum triangle/saw LFO, validated mode, placed at P:$0001.

    Rewritten 2026-07-28.  Every opcode confirmed via circuit-disasm.exe:

      0212DF  move x:(r2+<$5),b   the drum's packed centre|mode via r2, which
                                  replaces the old 27-word compare/load chain
      017F8E  and #<$3f,b         short-immediate forms replace 0140xx pairs
      014C8D  cmp #<$c,b
      01538D  cmp #<$13,b         valid range is now 12..19
      208E00  move x0,a           mode into A for the shape test
      014F85  cmp #<$f,a          >15 selects the saw path
      20002A  asr b               saw: signed ramp halved to +/-0.5
      2E8000  move #$80,a         immediate-short positions $80 in the top
                                  byte of A1 -> A = -1.0 full-wet low-pass

    Modes 12..15 are the original triangle speeds.  Modes 16..19 are sawtooth
    at the same shift counts, so their periods run 256..32 blocks (~5.9..47 Hz
    at 48 kHz / 32) -- the top saw speeds reach audio-rate growl deliberately.
    Both shapes span centre * (0.0 .. 2.0), so LFO_COEFFICIENT_MIN/MAX and the
    final accumulator-to-data move are unchanged.  A is never touched before
    the intentional final overwrite, so the off path returns the caller's
    amount without any save/restore and n7 is no longer used.  Cold boot
    leaves offset 5 zeroed -> mode 0 -> off path -> bit-exact stock.
    """

    words: list[int] = []
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(name: str) -> None:
        labels[name] = DSP_LFO_PC + len(words)

    def emit(*values: int) -> None:
        words.extend(values)

    def branch(opcode: int, target: str) -> None:
        emit(opcode, 0)
        fixups.append((len(words) - 1, target))

    emit(0x0212DF)              # move x:(r2+<$5),b   packed centre|mode
    emit(0x21E600)              # move b,y0           packed saved for depth
    emit(0x017F8E)              # and #<$3f,b         extract mode
    emit(0x014C8D)              # cmp #<$c,b
    branch(0x0AF0A9, "off")     # jlt >off
    emit(0x01538D)              # cmp #<$13,b
    branch(0x0AF0A7, "off")     # jgt >off

    emit(0x21E400)              # move b,x0           shift count = mode
    emit(0x57F000, 0x000065)    # move x:>$65,b       audio-block counter
    emit(0x0C1E59)              # asl x0,b,b
    emit(0x21A500)              # move b1,x1          wrap, not saturate
    emit(0x200069)              # tfr x1,b
    emit(0x208E00)              # move x0,a           a = mode for shape test
    emit(0x014F85)              # cmp #<$f,a
    branch(0x0AF0A7, "saw")     # jgt >saw

    emit(0x20002E)              # abs b               triangle
    emit(0x0140CC, 0x400000)    # sub #>$400000,b     -> -0.5 .. +0.5
    branch(0x0AF080, "mod")     # jmp >mod
    mark("saw")
    emit(0x20002A)              # asr b               ramp -> -0.5 .. +0.5
    mark("mod")
    emit(0x21A500)              # move b1,x1
    emit(0x2000E8)              # mpy x1,y0,b         centre-proportional depth
    emit(0x20003A)              # asl b               doubled sweep 0..2x
    emit(0x200058)              # add y0,b
    emit(0x21E700)              # move b,y1           coefficient out
    emit(0x2E8000)              # move #$80,a         force full-wet low-pass
    emit(0x00000C)              # rts

    mark("off")
    emit(0x47F400, STOCK_COEFFICIENT)  # move #>$9999a,y1
    emit(0x00000C)              # rts

    for index, target in fixups:
        words[index] = labels[target]

    if DSP_LFO_PC + len(words) > DSP_LFO_END_PC:
        raise ValueError(
            f"DSP LFO needs {len(words)} words; cave has "
            f"{DSP_LFO_END_PC - DSP_LFO_PC}"
        )
    return words, labels

def build_arm_helpers() -> list[tuple[int, bytes, int, str]]:
    parameter_read = assemble_thumb(
        f"""
            push {{r2, lr}}
            movs r0, #0x10
            bl {STOCK_DRUM_CONTROL_GETTER:#x}
            mov r7, r0
            ldr r2, [sp]
            movw r0, #{FILTER_AMOUNT_REGISTER_BASE:#x}
            adds r0, r0, r2
            bl {DSP_GETTER:#x}
            asrs r4, r0, #25
            adds r4, #0x40
            pop {{r2, pc}}
        """,
        ARM_PARAMETER_READ_START,
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
        ARM_PARAMETER_RESTORE_START,
    )

    normal_center = assemble_thumb(
        f"""
            push {{r3, lr}}
            mov r3, r2
            bl {ARM_CENTER_MAP_START:#x}
            lsls r0, r0, #8
            mov r1, r3
            bl {DSP_SETTER:#x}
            movs r0, #0
            pop {{r3, pc}}
        """,
        ARM_NORMAL_CENTER_START,
    )

    filter_restore_exit = assemble_thumb(
        f"""
            ldr r1, [sp]
            mov r0, r4
            bl {ARM_PARAMETER_RESTORE_START:#x}
            mov r0, r4
            pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
        """,
        ARM_FILTER_RESTORE_EXIT,
    )
    filter_normal_exit = assemble_thumb(
        f"""
            mov r0, r7
            pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
        """,
        ARM_FILTER_NORMAL_EXIT,
    )
    filter_clear_exit = assemble_thumb(
        f"""
            movs r0, #0
            mov r1, r6
            bl {DSP_SETTER:#x}
            mov r0, r7
            pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
        """,
        ARM_FILTER_CLEAR_EXIT,
    )

    scale_reset = assemble_thumb(
        f"""
            push {{r0, r1, r2, r3, r4, lr}}
            ldr r0, ={STATE_SCALE_MODE:#x}
            ldrb r1, [r0]
            lsls r1, r1, #27
            bpl scale_reset_done
            bl {STOCK_DRUM_CONTROL_GETTER:#x}
            movs r1, #64
            bl {DISTORTION_CLEAR_PREDICATE:#x}
            cbz r0, scale_reset_done
            ldr r0, ={STATE_SCALE_MODE:#x}
            ldrb r1, [r0]
            bic r1, r1, #{SCALE_MODE_BIT:#x}
            strb r1, [r0]
        scale_reset_done:
            pop {{r0, r1, r2, r3, r4, lr}}
            b.w {MAPPED_PITCH_GETTER:#x}
        """,
        ARM_SCALE_RESET_START,
    )

    sample_reset = assemble_thumb(
        f"""
            cmp r0, #0
            bne sample_shift
            mov r0, r5
            movs r1, #127
            bl {DISTORTION_CLEAR_PREDICATE:#x}
            cbz r0, sample_normal

            movs r0, #0
            movw r1, #{SAMPLE_OFFSET_RAW_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}
            movs r0, #0
            movw r1, #{SAMPLE_OFFSET_DISPLACEMENT_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}
            mov r0, r5
            lsls r0, r0, #24
            movw r1, #{SAMPLE_DECAY_CACHE_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}
            mov r0, r5
            pop.w {{r4, r5, r6, r7, r8, pc}}
        sample_normal:
            b.w {SAMPLE_NORMAL_DECAY:#x}
        sample_shift:
            b.w {SAMPLE_SHIFT_PATH:#x}
        """,
        ARM_SAMPLE_RESET_START,
    )

    center_map = assemble_thumb(
        f"""
            mov r2, r0
            mul r0, r0, r2
            mul r0, r0, r2
            lsls r0, r0, #1
            movw r2, #{CENTER_COEFFICIENT_MIN:#x}
            adds r0, r0, r2
            bic r0, r0, #0x3f
            orrs r0, r1
            bx lr
        """,
        ARM_CENTER_MAP_START,
    )

    helpers = [
        (
            ARM_PARAMETER_READ_START,
            parameter_read,
            ARM_PARAMETER_READ_END,
            "stored-parameter read helper",
        ),
        (
            ARM_PARAMETER_RESTORE_START,
            parameter_restore,
            ARM_PARAMETER_RESTORE_END,
            "stored-parameter restore helper",
        ),
        (
            ARM_NORMAL_CENTER_START,
            normal_center,
            ARM_NORMAL_CENTER_END,
            "active Filter centre setter",
        ),
        (
            ARM_FILTER_RESTORE_EXIT,
            filter_restore_exit,
            ARM_FILTER_NORMAL_EXIT,
            "Filter Shift restore exit",
        ),
        (
            ARM_FILTER_NORMAL_EXIT,
            filter_normal_exit,
            ARM_FILTER_CLEAR_EXIT,
            "stock Filter exit",
        ),
        (
            ARM_FILTER_CLEAR_EXIT,
            filter_clear_exit,
            ARM_FILTER_EXITS_END,
            "Filter Clear+Shift exit",
        ),
        (
            ARM_SCALE_RESET_START,
            scale_reset,
            ARM_SCALE_RESET_END,
            "Scale Follow Clear+Shift reset",
        ),
        (
            ARM_SAMPLE_RESET_START,
            sample_reset,
            ARM_SAMPLE_RESET_END,
            "Sample Start Clear+Shift reset",
        ),
        (
            ARM_CENTER_MAP_START,
            center_map,
            ARM_CENTER_MAP_END,
            "cubic Filter centre mapping",
        ),
    ]
    for address, code, end, purpose in helpers:
        if address + len(code) > end:
            raise ValueError(
                f"{purpose} needs {len(code)} bytes; slot has {end - address}"
            )
    return helpers


def build_panel_event_hook() -> bytes:
    """Arm Clear through the real panel event while preserving Scale Follow."""

    code = assemble_thumb(
        f"""
            push {{r3, r4, r5, r6, r7, lr}}
            push {{r0, r1, r2}}
            ldr r2, ={STATE_SCALE_MODE:#x}
            ldrb r0, [r2]
            lsls r3, r0, #26
            bmi panel_mode_initialized
            orr r0, r0, #{SCALE_MODE_BIT | SCALE_INITIALIZED_BIT:#x}
            strb r0, [r2]
            bl {ARM_REFRESH_ALL_DRUM_PITCH:#x}
            ldr r1, [sp, #4]
        panel_mode_initialized:
            ldrb r0, [r2]
            cmp r1, #{SHIFT_LOGICAL_ID}
            beq arm_shift_event
            cmp r1, #0x12
            beq panel_event_done

            movs r3, #{0x40 | CLEAR_ARMED_BIT:#x}
            bics r0, r3
            subs r1, #{CLEAR_EVENT_ID}
            it eq
            addeq r0, #{CLEAR_ARMED_BIT}
            strb r0, [r2]
            b panel_event_done

        arm_shift_event:
            orr r0, r0, #0x40
            strb r0, [r2]

        panel_event_done:
            pop {{r0, r1, r2}}
            mov r4, r0
            b.w {ARM_MAIN_HANDLER_RESUME:#x}
        """,
        ARM_PANEL_EVENT_HOOK_START,
    )
    capacity = ARM_PANEL_EVENT_HOOK_END - ARM_PANEL_EVENT_HOOK_START
    if len(code) != capacity:
        raise ValueError(
            f"panel-event hook is {len(code)} bytes; fixed slot is {capacity}"
        )
    return code


def build_arm_code() -> tuple[list[tuple[int, bytes]], bytes]:
    entries: list[tuple[int, bytes]] = []
    for index in range(4):
        address = ARM_CAVE_START + index * 4
        code = assemble_thumb(
            f"""
                movs r2, #{index}
                b {ARM_COMMON_START:#x}
            """,
            address,
        )
        if len(code) != 4:
            raise ValueError("filter LFO entry is not four bytes")
        entries.append((address, code))

    common = assemble_thumb(
        f"""
            push {{r1, r2, r3, r4, r5, r6, r7, lr}}
            mov r6, r2

            movw r0, #{LFO_MODE_REGISTERS[0]:#x}
            adds r0, r0, r6
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
            movs r1, #64
            bl {DISTORTION_CLEAR_PREDICATE:#x}
            cbnz r0, clear_reset

            and r1, r5, #0x3f
            cmp r1, #{LFO_MODE_MIN}
            blo normal_amount
            cmp r1, #{LFO_MODE_MAX}
            bhi normal_amount
            and r1, r5, #0x3f
            mov r0, r7
            mov r2, r6
            bl {ARM_NORMAL_CENTER_START:#x}
            mov r0, r7
            b wrapper_return

        shift_event:
            ldr r1, [sp]
            ldr r2, [sp, #4]
            bl {ARM_PARAMETER_READ_START:#x}

            and r0, r5, #0x3f
            cmp r0, #{LFO_MODE_MIN}
            blo mode_off
            cmp r0, #{LFO_MODE_MAX}
            bls mode_valid
        mode_off:
            movs r0, #0
        mode_valid:
            cmp r7, r4
            beq restore_amount
            blo slower_or_off

            cmp r0, #0
            bne faster
            mov r0, r4
            movs r1, #{LFO_MODE_MIN}
            bl {ARM_CENTER_MAP_START:#x}
            b store_state
        faster:
            cmp r0, #{LFO_MODE_MAX}
            bhs restore_amount
            adds r0, #1
            bic r1, r5, #0x3f
            orrs r0, r1
            b store_state

        slower_or_off:
            cmp r0, #{LFO_MODE_MIN}
            bls select_off
            subs r0, #1
            bic r1, r5, #0x3f
            orrs r0, r1
            b store_state
        select_off:
            movs r0, #0

        store_state:
            lsls r0, r0, #8
            mov r1, r6
            bl {DSP_SETTER:#x}
            b restore_amount

        clear_reset:
            b.w {ARM_FILTER_CLEAR_EXIT:#x}

        restore_amount:
            b.w {ARM_FILTER_RESTORE_EXIT:#x}

        normal_amount:
            b.w {ARM_FILTER_NORMAL_EXIT:#x}
        wrapper_return:
            pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
        """,
        ARM_COMMON_START,
    )
    if ARM_COMMON_START + len(common) > ARM_CAVE_END:
        raise ValueError(
            f"ARM wrapper needs {len(common)} bytes after entries; capacity is "
            f"{ARM_CAVE_END - ARM_COMMON_START}"
        )
    return entries, common


def build_distortion_reset_slot() -> bytes:
    common = assemble_thumb(
        f"""
            push {{r1, r2, r3, r4, r5, r6, r7, lr}}
            mov r5, r1
            mov r6, r2
            bl {STOCK_DRUM_CONTROL_GETTER:#x}
            mov r7, r0

            movs r0, #{SHIFT_LOGICAL_ID}
            bl {STOCK_BUTTON_PRESSED:#x}
            cbnz r0, select_type

            mov r0, r7
            movs r1, #0
            bl {DISTORTION_CLEAR_PREDICATE:#x}
            cbz r0, normal_amount
            movs r0, #0
            movw r1, #{DISTORTION_TYPE_BASE:#x}
            adds r1, r1, r6
            bl {DSP_SETTER:#x}
            mov r0, r7
            b selector_return

        select_type:
            movw r0, #0x8101
            adds r0, r0, r6
            bl {DSP_GETTER:#x}
            lsrs r4, r0, #24

            movw r0, #{DISTORTION_TYPE_BASE:#x}
            adds r0, r0, r6
            mov r6, r0
            bl {DSP_GETTER:#x}
            lsrs r0, r0, #8
            cmp r0, #6
            bls current_type_valid
            movs r0, #0
        current_type_valid:
            movs r2, #0
            cmp r7, r4
            beq restore_amount
            blo select_previous
            adds r0, #1
            cmp r0, #7
            bne type_selected
            movs r0, #0
            b type_selected
        select_previous:
            cbnz r0, decrement_type
            movs r0, #7
        decrement_type:
            subs r0, #1
        type_selected:
            movs r2, #1
            mov r7, r0

        restore_amount:
            ldr r0, ={PARAMETER_CONTEXT_POINTER:#x}
            ldr r0, [r0]
            ldr r0, [r0, #0x318]
            ldr r1, [r0, #4]
            add.w r1, r1, r5, lsl #3
            ldrh r1, [r1]
            ldr r0, [r0, #0x0c]
            ldr r0, [r0]
            strb r4, [r0, r1]

            cmp r2, #0
            beq return_old_amount
            mov r0, r7
            lsl.w r0, r0, #8
            mov r1, r6
            bl {DSP_SETTER:#x}
        return_old_amount:
            mov r0, r4
            b selector_return

        normal_amount:
            mov r0, r7
        selector_return:
            pop {{r1, r2, r3, r4, r5, r6, r7, pc}}
        """,
        DISTORTION_ARM_COMMON,
    )
    predicate = assemble_thumb(
        f"""
            cmp r0, r1
            bne clear_false
            push {{lr}}
            movs r0, #{CLEAR_HELD_ID:#x}
            bl {STOCK_BUTTON_PRESSED:#x}
            cbz r0, clear_zero
            movs r0, #1
        clear_zero:
            pop {{pc}}
        clear_false:
            movs r0, #0
            bx lr
        """,
        DISTORTION_CLEAR_PREDICATE,
    )
    common_capacity = DISTORTION_CLEAR_PREDICATE - DISTORTION_ARM_COMMON
    predicate_capacity = DISTORTION_ARM_SLOT_END - DISTORTION_CLEAR_PREDICATE
    if len(common) > common_capacity or (common_capacity - len(common)) % 2:
        raise ValueError(
            f"distortion reset wrapper needs {len(common)} bytes; "
            f"slot has {common_capacity}"
        )
    if len(predicate) > predicate_capacity or (predicate_capacity - len(predicate)) % 2:
        raise ValueError(
            f"Clear predicate needs {len(predicate)} bytes; "
            f"slot has {predicate_capacity}"
        )
    return (
        common
        + bytes.fromhex("00 BF") * ((common_capacity - len(common)) // 2)
        + predicate
        + bytes.fromhex("00 BF")
        * ((predicate_capacity - len(predicate)) // 2)
    )


def filter_lfo_allowed_offsets() -> set[int]:
    words, _ = build_dsp_lfo_words()
    entries, common = build_arm_code()
    helpers = build_arm_helpers()
    allowed = set(
        range(
            image_offset(DSP_LFO_ADDRESS),
            image_offset(DSP_LFO_ADDRESS) + len(dsp_bytes(words)),
        )
    )
    allowed.update(
        range(
            image_offset(DSP_HOOK_ADDRESS),
            image_offset(DSP_HOOK_ADDRESS) + 6,
        )
    )
    for address, code in entries:
        allowed.update(
            range(image_offset(address), image_offset(address) + len(code))
        )
    for address, code, _end, _purpose in helpers:
        allowed.update(
            range(image_offset(address), image_offset(address) + len(code))
        )
    allowed.update(
        range(
            image_offset(ARM_COMMON_START),
            image_offset(ARM_COMMON_START) + len(common),
        )
    )
    for hook in FILTER_HOOKS:
        allowed.update(
            range(image_offset(hook.address), image_offset(hook.address) + 4)
        )
    for address, _expected in SCALE_LIVE_HOOKS:
        allowed.update(range(image_offset(address), image_offset(address) + 4))
    allowed.update(
        range(
            image_offset(SAMPLE_MODIFIER_BRANCH),
            image_offset(SAMPLE_MODIFIER_BRANCH) + 4,
        )
    )
    allowed.update(
        range(
            image_offset(DISTORTION_ARM_COMMON),
            image_offset(DISTORTION_ARM_SLOT_END),
        )
    )
    allowed.update(
        range(
            image_offset(ARM_PANEL_EVENT_HOOK_START),
            image_offset(ARM_PANEL_EVENT_HOOK_END),
        )
    )
    return allowed


def apply_filter_lfo(base_image: bytes) -> tuple[bytes, dict]:
    if len(base_image) != IMAGE_SIZE:
        raise ValueError(f"unexpected image size {len(base_image):#x}")
    if sha256(base_image) != INPUT_IMAGE_SHA256:
        raise ValueError("filter LFO input is not exact hardware-tested v0.4.2")

    image = bytearray(base_image)
    dsp_words, dsp_labels = build_dsp_lfo_words()
    dsp_code = dsp_bytes(dsp_words)
    entries, common = build_arm_code()
    helpers = build_arm_helpers()
    distortion_slot = build_distortion_reset_slot()
    panel_event_hook = build_panel_event_hook()

    dsp_region = image[
        image_offset(DSP_LFO_ADDRESS) :
        image_offset(DSP_LFO_ADDRESS) + len(dsp_code)
    ]
    if any(dsp_region):
        raise ValueError("DSP LFO cave is not stock-zero in v0.4.2")
    image[
        image_offset(DSP_LFO_ADDRESS) :
        image_offset(DSP_LFO_ADDRESS) + len(dsp_code)
    ] = dsp_code

    arm_end = ARM_COMMON_START + len(common)
    for start, end, label in (
        (ARM_CAVE_START, ARM_FIRST_CAVE_END, "first"),
        (ARM_COMMON_START, ARM_CAVE_END, "second"),
    ):
        arm_region = image[image_offset(start) : image_offset(end)]
        if any(value != ARM_CAVE_FILL for value in arm_region):
            raise ValueError(f"{label} ARM LFO cave is not 0xFF in v0.4.2")
    for address, code, _end, purpose in helpers:
        region = image[image_offset(address) : image_offset(address) + len(code)]
        expected = (
            ARM_CAVE_FILL
            if ARM_CAVE_START <= address < ARM_FIRST_CAVE_END
            else 0
        )
        if any(value != expected for value in region):
            raise ValueError(f"{purpose} slot is not available in v0.4.2")
    for address, code in entries:
        image[image_offset(address) : image_offset(address) + len(code)] = code
    for address, code, _end, _purpose in helpers:
        image[image_offset(address) : image_offset(address) + len(code)] = code
    image[
        image_offset(ARM_COMMON_START) : image_offset(ARM_COMMON_START) + len(common)
    ] = common

    for address, expected in SCALE_LIVE_HOOKS:
        patch_bytes(
            image,
            address,
            expected,
            assemble_thumb(f"bl {ARM_SCALE_RESET_START:#x}", address),
        )
    patch_bytes(
        image,
        SAMPLE_MODIFIER_BRANCH,
        SAMPLE_MODIFIER_STOCK,
        assemble_thumb(f"b.w {ARM_SAMPLE_RESET_START:#x}", SAMPLE_MODIFIER_BRANCH),
    )
    image[
        image_offset(DISTORTION_ARM_COMMON) :
        image_offset(DISTORTION_ARM_SLOT_END)
    ] = distortion_slot
    patch_bytes(
        image,
        ARM_PANEL_EVENT_HOOK_START,
        BASE_PANEL_EVENT_HOOK,
        panel_event_hook,
    )

    dsp_hook = dsp_bytes([0x0BF080, DSP_LFO_PC])
    patch_bytes(
        image,
        DSP_HOOK_ADDRESS,
        DSP_STOCK_COEFFICIENT_LOAD,
        dsp_hook,
    )

    hooks = []
    for index, hook in enumerate(FILTER_HOOKS):
        target = ARM_CAVE_START + index * 4
        replacement = assemble_thumb(f"bl {target:#x}", hook.address)
        patch_bytes(image, hook.address, hook.stock, replacement)
        hooks.append(
            {
                "drum": hook.drum,
                "address": f"{hook.address:#010x}",
                "target": f"{target:#010x}",
                "parameter_id": f"{hook.parameter_id:#04x}",
                "amount_register": f"Y:${0x105 + index:04X}",
                "mode_register": f"X:${LFO_MODE_REGISTERS[index]:04X}",
            }
        )

    changed = {
        index
        for index, (before, after) in enumerate(zip(base_image, image))
        if before != after
    }
    unexpected = sorted(changed - filter_lfo_allowed_offsets())
    if unexpected:
        raise ValueError(
            f"filter LFO changed undeclared offsets: "
            f"{[hex(value) for value in unexpected[:16]]}"
        )

    manifest = {
        "feature": "per-drum centre-frequency triangle filter LFO and stock-style resets",
        "status": "NOT hardware-validated",
        "base": "exact hardware-validated v0.4.2",
        "gesture": (
            "hold Shift and turn Macro 7/8; clockwise enables/accelerates, "
            "counter-clockwise slows and then disables"
        ),
        "normal_filter": "stock while LFO is off; centre cutoff while LFO is on",
        "active_filter": "full-wet low-pass with centre-proportional triangle depth",
        "centre_mapping": (
            "30 Hz..5.2 kHz centre from Macro 7/8 via "
            "coefficient = 0x008000 + 2*raw^3"
        ),
        "phase_source": "DSP X:$0065 audio-block counter",
        "mode_values": {
            "off": 0,
            "slow": 12,
            "medium": 13,
            "fast": 14,
            "very_fast": 15,
        },
        "approximate_rates_hz": [0.366, 0.732, 1.465, 2.930],
        "coefficient_range": [
            f"0x{LFO_COEFFICIENT_MIN:06X}",
            f"0x{LFO_COEFFICIENT_MAX:06X}",
        ],
        "approximate_cutoff_range_hz": [15, 10250],
        "reset_gestures": {
            "Pitch Macro 1/2": "stock Clear+Macro reset also turns Scale Follow off",
            "Decay Macro 3/4": "stock Clear+Macro reset also zeros Sample Start",
            "Distortion Macro 5/6": "stock Clear+Macro reset also selects type zero",
            "Filter Macro 7/8": "stock Clear+Macro reset also disables the LFO",
        },
        "reset_led": "stock Macro LED handling is untouched; factory blue remains stock",
        "mode_registers": [
            f"X:${address:04X}" for address in LFO_MODE_REGISTERS
        ],
        "dsp_hook_pc": f"P:${DSP_HOOK_PC:04X}",
        "dsp_lfo_pc": f"P:${DSP_LFO_PC:04X}",
        "dsp_lfo_words": len(dsp_words),
        "dsp_labels": {
            name: f"P:${address:04X}" for name, address in dsp_labels.items()
        },
        "arm_entries": f"{ARM_CAVE_START:#010x}..{ARM_SCALE_RESET_START:#010x}",
        "arm_common": f"{ARM_COMMON_START:#010x}..{arm_end:#010x}",
        "arm_common_bytes": len(common),
        "changed_bytes": len(changed),
        "hooks": hooks,
    }
    return bytes(image), manifest
