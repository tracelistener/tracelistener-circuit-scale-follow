"""Static and Thumb-emulated verification for tagged Shift automation.

Run from the repository root:

    python experimental\\verify_circuit_shift_automation.py

Emulates the shared playback decoder against the real patched image and checks
that untagged automation is bit-for-bit stock while tagged automation applies
hidden state without ever reaching the stock parameter dispatcher.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parent
ROOT = ANALYSIS_DIR.parent
PUBLISHED_TOOLS = ROOT / "tools"

sys.path.insert(0, str(ANALYSIS_DIR / "unicorn_lib"))
sys.path.insert(0, str(ANALYSIS_DIR / "capstone_local"))
sys.path.insert(0, str(ANALYSIS_DIR / "keystone_lib"))
sys.path.insert(0, str(ANALYSIS_DIR))
sys.path.insert(0, str(PUBLISHED_TOOLS))
sys.stdout.reconfigure(encoding="utf-8")

from capstone import CS_ARCH_ARM, CS_MODE_LITTLE_ENDIAN, CS_MODE_THUMB, Cs
from unicorn import UC_ARCH_ARM, UC_HOOK_CODE, UC_MODE_THUMB, Uc
from unicorn.arm_const import (
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R2,
    UC_ARM_REG_R3,
    UC_ARM_REG_R4,
    UC_ARM_REG_R5,
    UC_ARM_REG_R6,
    UC_ARM_REG_R7,
    UC_ARM_REG_SP,
)

import build_circuit_scale_follow as release
import circuit_shift_automation_patch as mod
import circuit_filter_lfo_patch as lfo
from circuit_fw_tools import decode_firmware, encode_firmware, load

V042_IMAGE = "e6a2b77bea0918e28b2cdadca6bef96654d6a39d7272b3a1f9ac25a51815cca2"

FLASH_BASE = 0x08000000
FLASH_SIZE = 0x00040000
RAM_BASE = 0x20000000
RAM_SIZE = 0x00020000
STACK = 0x2001EF00
RETURN_PC = 0x08007000

# Fake addresses used only to observe control flow.
TABLE_BASE = 0x20009000
STOCK_SETTER_STUB = 0x0800CC59  # thumb bit set; the decoder tail-calls this


class DecoderEmulator:
    """Drive the shared playback decoder exactly as the stock sites do."""

    def __init__(self, image: bytes, *, automation_byte: int, parameter_id: int):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE)
        self.uc.mem_write(release.BASE, image)
        self.uc.mem_map(RAM_BASE, RAM_SIZE)
        self.automation_byte = automation_byte
        self.parameter_id = parameter_id
        self.dsp: dict[int, int] = {}
        self.setter_calls: list[tuple[int, int]] = []
        self.getter_calls: list[int] = []
        self.dispatcher_calls = 0
        self.dispatcher_args: tuple[int, int, int] | None = None
        self.uc.hook_add(UC_HOOK_CODE, self._hook)
        # The parameter-id table the stock code indexes with the lane index.
        self.uc.mem_write(TABLE_BASE, bytes((parameter_id,)))

    @staticmethod
    def _return(uc: Uc) -> None:
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def _hook(self, uc: Uc, address: int, size: int, _: object) -> None:
        if address == RETURN_PC:
            uc.emu_stop()
            return
        if address == (STOCK_SETTER_STUB & ~1):
            # The decoder tail-called the stock dispatcher.
            self.dispatcher_calls += 1
            self.dispatcher_args = (
                uc.reg_read(UC_ARM_REG_R0),
                uc.reg_read(UC_ARM_REG_R1),
                uc.reg_read(UC_ARM_REG_R2),
            )
            self._return(uc)
            return
        if address == mod.DSP_GETTER:
            register = uc.reg_read(UC_ARM_REG_R0) & 0xFFFF
            self.getter_calls.append(register)
            value = self.dsp.get(register, 0)
            uc.reg_write(UC_ARM_REG_R1, 0xDEADBE11)
            uc.reg_write(UC_ARM_REG_R2, 0xDEADBE12)
            uc.reg_write(UC_ARM_REG_R3, 0xDEADBE13)
            uc.reg_write(UC_ARM_REG_R0, value)
            self._return(uc)
            return
        if address == mod.DSP_SETTER:
            register = uc.reg_read(UC_ARM_REG_R1) & 0xFFFF
            value = uc.reg_read(UC_ARM_REG_R0) & 0xFFFFFFFF
            self.dsp[register] = value
            self.setter_calls.append((register, value))
            uc.reg_write(UC_ARM_REG_R1, 0xDEADBE21)
            uc.reg_write(UC_ARM_REG_R2, 0xDEADBE22)
            uc.reg_write(UC_ARM_REG_R3, 0xDEADBE23)
            self._return(uc)

    def run(self) -> None:
        uc = self.uc
        uc.reg_write(UC_ARM_REG_SP, STACK)
        uc.reg_write(UC_ARM_REG_LR, RETURN_PC | 1)
        uc.reg_write(UC_ARM_REG_R0, 0x20008000)  # parameter object
        uc.reg_write(UC_ARM_REG_R2, self.automation_byte)
        uc.reg_write(UC_ARM_REG_R3, TABLE_BASE)
        uc.reg_write(UC_ARM_REG_R5, 0)
        uc.reg_write(UC_ARM_REG_R6, STOCK_SETTER_STUB)
        uc.emu_start(mod.PLAYBACK_DECODER | 1, RETURN_PC, count=4000)


class RecordEmulator:
    """Drive the record helper exactly as the hook at 0x0801CD6E does.

    Entered by `bl` with r1 = parameter id, r2 = mode selector, r5 = lane index,
    r6 = the value the stock code is about to store, and LR pointing at
    0x0801CD72.  The helper must preserve r0/r1/r3/r4/r5/r7, reproduce the
    displaced `add.w r2,r2,#0x16`, leave the unsigned flags equivalent to the
    `cmp r2,#1` at 0x0801CD6C, and touch r6 only when it has produced a tag.
    """

    SENTINEL = 0x08007100

    def __init__(
        self,
        image: bytes,
        *,
        parameter_id: int,
        mode: int,
        value: int,
        shift_held: bool,
    ):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE)
        self.uc.mem_write(release.BASE, image)
        self.uc.mem_map(RAM_BASE, RAM_SIZE)
        self.parameter_id = parameter_id
        self.mode = mode
        self.value = value
        self.shift_held = shift_held
        self.dsp: dict[int, int] = {}
        self.faults: list[str] = []
        self.uc.hook_add(UC_HOOK_CODE, self._hook)

    @staticmethod
    def _return(uc: Uc) -> None:
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def _hook(self, uc: Uc, address: int, size: int, _: object) -> None:
        if address == self.SENTINEL:
            uc.emu_stop()
            return
        if address == mod.STOCK_BUTTON_PRESSED:
            # AAPCS: r0-r3 are caller-saved and the real routine does clobber
            # them.  A stub that preserved r1 hid a fatal defect -- the helper
            # read the parameter id from r1 after this call and decoded garbage.
            # Poison them so any such dependency fails here instead of on
            # hardware.
            uc.reg_write(UC_ARM_REG_R1, 0xDEADBE01)
            uc.reg_write(UC_ARM_REG_R2, 0xDEADBE02)
            uc.reg_write(UC_ARM_REG_R3, 0xDEADBE03)
            uc.reg_write(UC_ARM_REG_R0, int(self.shift_held))
            self._return(uc)
            return
        if address == mod.DSP_GETTER:
            register = uc.reg_read(UC_ARM_REG_R0) & 0xFFFF
            value = self.dsp.get(register, 0)
            uc.reg_write(UC_ARM_REG_R1, 0xDEADBE11)
            uc.reg_write(UC_ARM_REG_R2, 0xDEADBE12)
            uc.reg_write(UC_ARM_REG_R3, 0xDEADBE13)
            uc.reg_write(UC_ARM_REG_R0, value)
            self._return(uc)
            return
        if address == mod.DSP_SETTER:
            self.faults.append("recorder wrote DSP state; it must only read")
            self._return(uc)

    def run(self) -> dict[str, int]:
        uc = self.uc
        uc.reg_write(UC_ARM_REG_SP, STACK)
        uc.reg_write(UC_ARM_REG_LR, self.SENTINEL | 1)
        preserved = {
            UC_ARM_REG_R0: 0xA0A0A0A0,
            UC_ARM_REG_R3: 0xA3A3A3A3,
            UC_ARM_REG_R4: 0xA4A4A4A4,
            UC_ARM_REG_R7: 0xA7A7A7A7,
        }
        for register, filler in preserved.items():
            uc.reg_write(register, filler)
        uc.reg_write(UC_ARM_REG_R1, self.parameter_id)
        uc.reg_write(UC_ARM_REG_R2, self.mode)
        uc.reg_write(UC_ARM_REG_R5, 5)
        uc.reg_write(UC_ARM_REG_R6, self.value)
        uc.emu_start(mod.ARM_RECORD_HELPER | 1, self.SENTINEL, count=4000)

        for register, filler in preserved.items():
            if uc.reg_read(register) != filler:
                self.faults.append(f"recorder clobbered a preserved register ({register})")
        if uc.reg_read(UC_ARM_REG_R1) != self.parameter_id:
            self.faults.append("recorder clobbered r1")
        if uc.reg_read(UC_ARM_REG_R5) != 5:
            self.faults.append("recorder clobbered r5")
        return {
            "r2": uc.reg_read(UC_ARM_REG_R2),
            "r6": uc.reg_read(UC_ARM_REG_R6),
        }


class FilterWrapperEmulator:
    """Drive the live Filter callback, including active-low Clear handling."""

    SENTINEL = 0x08007200

    def __init__(
        self,
        image: bytes,
        *,
        proposed: int,
        shift_held: bool = False,
        clear_held: bool = False,
    ):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(FLASH_BASE, FLASH_SIZE)
        self.uc.mem_write(release.BASE, image)
        self.uc.mem_map(RAM_BASE, RAM_SIZE)
        self.proposed = proposed
        self.shift_held = shift_held
        self.clear_held = clear_held
        self.dsp: dict[int, int] = {}
        self.setter_calls: list[tuple[int, int]] = []
        self.button_calls: list[tuple[int, int]] = []
        self.uc.hook_add(UC_HOOK_CODE, self._hook)

    @staticmethod
    def _return(uc: Uc) -> None:
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def _hook(self, uc: Uc, address: int, size: int, _: object) -> None:
        if address == self.SENTINEL:
            uc.emu_stop()
            return
        if address == mod.STOCK_DRUM_CONTROL_GETTER:
            uc.reg_write(UC_ARM_REG_R0, self.proposed)
            uc.reg_write(UC_ARM_REG_R1, 0xDEADCA01)
            uc.reg_write(UC_ARM_REG_R2, 0xDEADCA02)
            uc.reg_write(UC_ARM_REG_R3, 0xDEADCA03)
            self._return(uc)
            return
        if address == mod.STOCK_BUTTON_PRESSED:
            logical_id = uc.reg_read(UC_ARM_REG_R0) & 0xFF
            if logical_id == mod.SHIFT_LOGICAL_ID:
                result = int(self.shift_held)
            elif logical_id == mod.CLEAR_LOGICAL_ID:
                # Clear's stock scan bit is active-low: high at rest, low held.
                result = int(not self.clear_held)
            else:
                raise ValueError(f"unexpected logical button query {logical_id:#x}")
            self.button_calls.append((logical_id, result))
            uc.reg_write(UC_ARM_REG_R0, result)
            uc.reg_write(UC_ARM_REG_R1, 0xDEADCB01)
            uc.reg_write(UC_ARM_REG_R2, 0xDEADCB02)
            uc.reg_write(UC_ARM_REG_R3, 0xDEADCB03)
            self._return(uc)
            return
        if address == mod.DSP_GETTER:
            register = uc.reg_read(UC_ARM_REG_R0) & 0xFFFF
            uc.reg_write(UC_ARM_REG_R0, self.dsp.get(register, 0))
            uc.reg_write(UC_ARM_REG_R1, 0xDEADCC01)
            uc.reg_write(UC_ARM_REG_R2, 0xDEADCC02)
            uc.reg_write(UC_ARM_REG_R3, 0xDEADCC03)
            self._return(uc)
            return
        if address == mod.DSP_SETTER:
            register = uc.reg_read(UC_ARM_REG_R1) & 0xFFFF
            value = uc.reg_read(UC_ARM_REG_R0) & 0xFFFFFFFF
            self.dsp[register] = value
            self.setter_calls.append((register, value))
            uc.reg_write(UC_ARM_REG_R0, 0xDEADCD00)
            uc.reg_write(UC_ARM_REG_R1, 0xDEADCD01)
            uc.reg_write(UC_ARM_REG_R2, 0xDEADCD02)
            uc.reg_write(UC_ARM_REG_R3, 0xDEADCD03)
            self._return(uc)

    def run(self, drum: int) -> int:
        hook = lfo.FILTER_HOOKS[drum]
        preserved = {
            UC_ARM_REG_R1: hook.parameter_id,
            UC_ARM_REG_R3: 0x33333333,
            UC_ARM_REG_R4: 0x44444444,
            UC_ARM_REG_R5: 0x55555555,
            UC_ARM_REG_R6: 0x66666666,
            UC_ARM_REG_R7: 0x77777777,
        }
        self.uc.reg_write(UC_ARM_REG_R0, 0x10)
        self.uc.reg_write(UC_ARM_REG_R2, 0x22222222)
        for register, value in preserved.items():
            self.uc.reg_write(register, value)
        self.uc.reg_write(UC_ARM_REG_SP, STACK)
        self.uc.reg_write(UC_ARM_REG_LR, self.SENTINEL | 1)
        self.uc.emu_start(
            (mod.ARM_LFO_ENTRIES + drum * 4) | 1,
            self.SENTINEL,
            count=4000,
        )
        for register, expected in preserved.items():
            if self.uc.reg_read(register) != expected:
                raise ValueError(f"Filter wrapper clobbered register {register}")
        return self.uc.reg_read(UC_ARM_REG_R0) & 0xFFFFFFFF


def record_checks(patched: bytes) -> dict[str, object]:
    results: dict[str, object] = {}
    untouched = 0x5A

    def run(
        parameter_id: int,
        *,
        shift: bool,
        mode: int = 0,
        value: int = untouched,
        dsp: dict[int, int] | None = None,
    ):
        emu = RecordEmulator(
            patched,
            parameter_id=parameter_id,
            mode=mode,
            value=value,
            shift_held=shift,
        )
        if dsp:
            emu.dsp.update(dsp)
        out = emu.run()
        if emu.faults:
            raise ValueError("; ".join(emu.faults))
        return out

    # Shift released: bit-for-bit stock behaviour on every parameter id.
    for parameter_id in range(0, 32):
        out = run(parameter_id, shift=False, mode=1)
        if out["r6"] != untouched:
            raise ValueError(f"Shift released but r6 changed for id {parameter_id}")
        if out["r2"] != 1 + 0x16:
            raise ValueError(f"displaced add.w not reproduced for id {parameter_id}")
    results["shift_released_is_stock"] = "32 parameter ids, r6 untouched, r2 = mode + 0x16"

    # Parameter ids outside the three hidden lanes leave r6 alone even with Shift.
    ignored = [0, 1, 2, 7, 8, 9, 27, 28, 0x20002DB0]
    for parameter_id in ignored:
        out = run(parameter_id, shift=True, mode=1)
        if out["r6"] != untouched:
            raise ValueError(f"id {parameter_id} produced a tag but is not a hidden lane")
    results["non_hidden_ids_ignored"] = ignored

    # Sample Start: raw upper byte, clamped to 126 because 0x80|127 is 0xFF.
    for drum in range(4):
        for raw, expect in ((0, 0), (1, 1), (63, 63), (126, 126), (127, 126)):
            out = run(
                3 + 7 * drum,
                shift=True,
                dsp={mod.SAMPLE_RAW_BASE + drum: raw << 24},
            )
            if out["r6"] != (0x80 | expect):
                raise ValueError(
                    f"sample drum {drum} raw {raw} tagged {out['r6']:#04x}, "
                    f"expected {0x80 | expect:#04x}"
                )
    results["sample_start_tags"] = "4 drums, endpoint 127 stored as 126"

    # Distortion: valid 0..6, anything else recorded as 0.
    for drum in range(4):
        for kind, expect in ((0, 0), (6, 6), (7, 0), (0xFF, 0)):
            out = run(
                4 + 7 * drum,
                shift=True,
                dsp={mod.DISTORTION_TYPE_BASE + drum: kind << 8},
            )
            if out["r6"] != (0x80 | expect):
                raise ValueError(f"distortion drum {drum} type {kind} tagged {out['r6']:#04x}")
    results["distortion_tags"] = "4 drums, 0..6 kept, out-of-range recorded as Off"

    # Filter LFO: Off or 12..19; the packed centre must not leak into the tag.
    centre = 0x3F0040
    for drum in range(4):
        for packed_mode, expect in ((0, 0), (11, 0), (12, 12), (15, 15), (16, 16), (19, 19), (20, 0)):
            out = run(
                5 + 7 * drum,
                shift=True,
                dsp={lfo.LFO_MODE_REGISTERS[drum]: ((centre & ~0x3F) | packed_mode) << 8},
            )
            if out["r6"] != (0x80 | expect):
                raise ValueError(
                    f"filter drum {drum} mode {packed_mode} tagged {out['r6']:#04x}, "
                    f"expected {0x80 | expect:#04x}"
                )
    results["filter_tags"] = "4 drums, Off/12..19 kept, out-of-range recorded as Off"

    # The hardware-observed callback order is recorder first, Filter wrapper
    # second.  Exercise the predictor against the wrapper's exact transition:
    # the first two encoder events retain the current mode, the third advances
    # or retreats, and equal-to-baseline events never move it.
    def amount_word(baseline: int) -> int:
        return ((baseline - 64) << 25) & 0xFFFFFFFF

    transition_cases = (
        # current, event, baseline, expected after a passing divider event
        (0, 70, 64, 12),
        (12, 70, 64, 13),
        (18, 70, 64, 19),
        (19, 70, 64, 19),
        (0, 58, 64, 0),
        (12, 58, 64, 0),
        (13, 58, 64, 12),
        (19, 58, 64, 18),
        (15, 64, 64, 15),
        (11, 70, 64, 12),
        (20, 58, 64, 0),
    )
    predicted = 0
    for drum in range(4):
        state_register = lfo.LFO_MODE_REGISTERS[drum]
        for current, event, baseline, passed_expect in transition_cases:
            valid_current = current if current == 0 or 12 <= current <= 19 else 0
            for counter in (0, 1, 2):
                expect = passed_expect if counter == 2 else valid_current
                out = run(
                    filter_id(drum),
                    shift=True,
                    value=event,
                    dsp={
                        state_register: (0x220000 | current) << 8,
                        mod.STEP_DIVIDER_STATE + drum: counter << 8,
                        mod.FILTER_AMOUNT_BASE + drum: amount_word(baseline),
                    },
                )
                if out["r6"] != (0x80 | expect):
                    raise ValueError(
                        f"filter predictor drum {drum}, current {current}, "
                        f"event {event}, counter {counter}: got {out['r6']:#04x}, "
                        f"expected {0x80 | expect:#04x}"
                    )
                predicted += 1
    results["filter_recorder_first_prediction"] = (
        f"{predicted} drum/direction/divider transitions match the live wrapper"
    )

    # Every tag must stay inside 0x80..0xFE so 0xFF keeps meaning empty.
    seen: set[int] = set()
    for drum in range(4):
        seen.add(run(3 + 7 * drum, shift=True, dsp={mod.SAMPLE_RAW_BASE + drum: 127 << 24})["r6"])
        seen.add(run(4 + 7 * drum, shift=True, dsp={mod.DISTORTION_TYPE_BASE + drum: 6 << 8})["r6"])
        seen.add(
            run(
                5 + 7 * drum,
                shift=True,
                dsp={lfo.LFO_MODE_REGISTERS[drum]: ((centre & ~0x3F) | 19) << 8},
            )["r6"]
        )
    if not seen or max(seen) > 0xFE or min(seen) < 0x80:
        raise ValueError(f"tag escaped 0x80..0xFE: {sorted(hex(v) for v in seen)}")
    results["tag_range_respected"] = sorted(hex(value) for value in seen)

    # The displaced instruction and its flag contract, across mode selectors.
    for mode in (0, 1, 2, 3):
        emu = RecordEmulator(
            patched, parameter_id=4, mode=mode, value=untouched, shift_held=False
        )
        out = emu.run()
        if out["r2"] != mode + 0x16:
            raise ValueError(f"mode {mode} became {out['r2'] - 0x16}")
        stock_unsigned = mode > 1
        replayed_unsigned = (mode + 0x16) > 0x17
        if stock_unsigned != replayed_unsigned:
            raise ValueError(f"flag contract broken for mode {mode}")
    results["displaced_add_and_flags"] = "mode 0..3 restored; bhi condition equivalent"
    return results


def sample_start_id(drum: int) -> int:
    return 3 + 7 * drum


def distortion_id(drum: int) -> int:
    return 4 + 7 * drum


def filter_id(drum: int) -> int:
    return 5 + 7 * drum


def static_checks(base: bytes, patched: bytes) -> dict[str, object]:
    results: dict[str, object] = {}

    if len(patched) != len(base):
        raise ValueError("image size changed")
    results["size_preserved"] = True

    clear_query = bytes.fromhex(
        "19 20 02 F0 48 F9 00 28 0C BF 03 20 02 20 00 E0"
    )
    clear_query_offset = mod.image_offset(0x0800A312)
    if base[clear_query_offset : clear_query_offset + len(clear_query)] != clear_query:
        raise ValueError("stock Clear query contract changed")
    results["stock_clear_query"] = (
        "logical ID 0x19; stock code distinguishes raw zero from nonzero"
    )

    disassembler = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
    branch_count = 0
    for start, end in (
        (mod.ARM_LFO_COMMON, mod.ARM_LFO_SECOND_END),
        (mod.ARM_NORMAL_CENTER, mod.ARM_NORMAL_CENTER_END),
    ):
        for instruction in disassembler.disasm(
            patched[mod.image_offset(start) : mod.image_offset(end)], start
        ):
            if not (
                instruction.mnemonic.startswith("b")
                and instruction.op_str.startswith("#")
            ):
                continue
            target = int(instruction.op_str[1:], 16)
            if not FLASH_BASE <= target < FLASH_BASE + FLASH_SIZE:
                raise ValueError(
                    f"inserted branch escapes flash at {instruction.address:#010x}: "
                    f"{target:#010x}"
                )
            branch_count += 1
    results["direct_branch_targets_checked"] = branch_count

    # Both playback sites now call the shared decoder.
    for name, address in (
        ("tick", mod.PLAYBACK_TICK_PATCH),
        ("reload", mod.PLAYBACK_RELOAD_PATCH),
    ):
        expected = mod.assemble_thumb(f"bl {mod.PLAYBACK_DECODER:#x}", address)
        actual = patched[mod.image_offset(address) : mod.image_offset(address) + 4]
        if actual != expected:
            raise ValueError(f"{name} playback site not redirected to the decoder")
    results["playback_sites_hooked"] = ["0x0800fb02", "0x08012c4e"]

    # The stock empty-automation test must be untouched immediately above each site.
    for address in (0x0800FAF4, 0x08012C40):
        window = patched[mod.image_offset(address) : mod.image_offset(address) + 4]
        if window != bytes.fromhex("F2 5C FF 2A"):
            raise ValueError(f"stock 0xFF automation test altered at {address:#010x}")
    results["empty_automation_test_intact"] = "ldrb r2,[r6,r3]; cmp r2,#0xff preserved"

    changed = {i for i, (a, b) in enumerate(zip(base, patched)) if a != b}
    allowed = mod.clean_lfo_allowed_offsets() | mod.automation_allowed_offsets()
    unexpected = sorted(changed - allowed)
    if unexpected:
        raise ValueError(f"writes outside declared regions: {[hex(o) for o in unexpected[:12]]}")
    results["changed_bytes"] = len(changed)
    results["writes_outside_declared_regions"] = 0
    return results


def packed_filter_state(raw: int, mode: int) -> int:
    coefficient = (lfo.CENTER_COEFFICIENT_MIN + 2 * raw**3) & ~0x3F
    return coefficient | mode


def emulation_checks(patched: bytes) -> dict[str, object]:
    results: dict[str, object] = {}

    # 1. Untagged values 0..127 must reach the stock dispatcher unchanged.
    for value in range(0, 128):
        emu = DecoderEmulator(patched, automation_byte=value, parameter_id=sample_start_id(0))
        emu.run()
        if emu.dispatcher_calls != 1:
            raise ValueError(f"untagged {value} did not call the stock dispatcher exactly once")
        if emu.dispatcher_args[2] != value:
            raise ValueError(f"untagged {value} was altered before the dispatcher")
        if emu.setter_calls:
            raise ValueError(f"untagged {value} touched hidden DSP state")
    results["untagged_0_127_passthrough"] = "128 values, dispatcher called once, DSP untouched"

    # 2. Tagged values must never reach the stock dispatcher.
    tagged_checked = 0
    for drum in range(4):
        for value in (0, 1, 6, 12, 15, 63, 126):
            emu = DecoderEmulator(
                patched,
                automation_byte=0x80 | value,
                parameter_id=distortion_id(drum),
            )
            emu.run()
            if emu.dispatcher_calls:
                raise ValueError("tagged value reached the stock parameter dispatcher")
            tagged_checked += 1
    results["tagged_suppresses_dispatcher"] = tagged_checked

    # 3. Distortion type per drum.
    for drum in range(4):
        for kind in range(7):
            emu = DecoderEmulator(
                patched, automation_byte=0x80 | kind, parameter_id=distortion_id(drum)
            )
            emu.run()
            register = mod.DISTORTION_TYPE_BASE + drum
            if emu.dsp.get(register) != kind << 8:
                raise ValueError(
                    f"distortion drum {drum} type {kind} wrote "
                    f"{emu.dsp.get(register)!r} to {register:#06x}"
                )
    results["distortion_types"] = "4 drums x 7 types written to X:$1E65..$1E68"

    # 4. Filter LFO mode replaces only the low six bits, preserving the centre.
    centre = 0x3F0040
    for drum in range(4):
        for mode in (0, 12, 13, 14, 15, 16, 19):
            emu = DecoderEmulator(
                patched, automation_byte=0x80 | mode, parameter_id=filter_id(drum)
            )
            register = lfo.LFO_MODE_REGISTERS[drum]
            emu.dsp[register] = (centre | 0x0C) << 8
            emu.run()
            written = emu.dsp.get(register)
            if written is None:
                raise ValueError(f"filter drum {drum} mode {mode} wrote nothing")
            decoded = (written >> 8) & 0xFFFFFF
            if decoded & 0x3F != mode:
                raise ValueError(f"filter drum {drum} mode {mode} decoded as {decoded & 0x3F}")
            if decoded & ~0x3F != centre & ~0x3F:
                raise ValueError(f"filter drum {drum} mode {mode} disturbed the centre")
    results["filter_modes"] = "4 drums x {off,12..15,16,19}, stride-10 regs, centre preserved"

    # 4b. No centre yet -> the ~596 Hz default is substituted, not silence.
    for drum in range(4):
        emu = DecoderEmulator(patched, automation_byte=0x80 | 14, parameter_id=filter_id(drum))
        emu.run()
        register = lfo.LFO_MODE_REGISTERS[drum]
        decoded = (emu.dsp.get(register, 0) >> 8) & 0xFFFFFF
        if decoded != (0x0A0000 | 14):
            raise ValueError(f"filter drum {drum} empty-centre default wrong: {decoded:#x}")
    results["filter_centre_default"] = "zero centre -> 0x0A0000 (~621 Hz) substituted"

    # 4c. Clear is the stock logical ID 0x19 but its scan bit is active-low.
    # Only the stock default value (64, the blue-LED clockwise reset) may turn
    # the hidden LFO off.  Other values must preserve the LFO so the stock
    # counter-clockwise automation-delete gesture remains independent.
    clear_results: dict[str, str] = {}
    for drum in range(4):
        register = lfo.LFO_MODE_REGISTERS[drum]
        mode = lfo.LFO_MODE_MIN + drum

        reset = FilterWrapperEmulator(patched, proposed=64, clear_held=True)
        reset.dsp[register] = packed_filter_state(51 + drum, mode) << 8
        if reset.run(drum) != 64:
            raise ValueError(f"Clear reset changed Drum {drum + 1}'s stock return")
        expected_off = packed_filter_state(64, 0) << 8
        if reset.dsp.get(register) != expected_off:
            raise ValueError(f"active-low Clear did not disable Drum {drum + 1}")
        if reset.button_calls != [
            (mod.SHIFT_LOGICAL_ID, 0),
            (mod.CLEAR_LOGICAL_ID, 0),
        ]:
            raise ValueError("Clear reset did not observe the active-low scan state")

        rest = FilterWrapperEmulator(patched, proposed=64, clear_held=False)
        rest.dsp[register] = packed_filter_state(51 + drum, mode) << 8
        rest.run(drum)
        expected_active = packed_filter_state(64, mode) << 8
        if rest.dsp.get(register) != expected_active:
            raise ValueError("Clear scan high-at-rest incorrectly disabled the LFO")
        if rest.button_calls[-1] != (mod.CLEAR_LOGICAL_ID, 1):
            raise ValueError("Clear high-at-rest state was not modelled")

        erase = FilterWrapperEmulator(patched, proposed=32, clear_held=True)
        erase.dsp[register] = packed_filter_state(51 + drum, mode) << 8
        erase.run(drum)
        if erase.dsp.get(register) != packed_filter_state(32, mode) << 8:
            raise ValueError("non-default Clear movement reset the hidden LFO")
        if erase.button_calls != [(mod.SHIFT_LOGICAL_ID, 0)]:
            raise ValueError("Clear was queried outside the stock default-value path")

        clear_results[f"drum_{drum + 1}"] = "value 64 + raw Clear 0 -> Off"
    results["filter_clear_active_low"] = clear_results

    # 5. Sample Start writes both the raw word and a displacement.
    for drum in range(4):
        for value in (0, 1, 63, 126, 127):
            emu = DecoderEmulator(
                patched, automation_byte=0x80 | (value & 0x7F), parameter_id=sample_start_id(drum)
            )
            emu.dsp[mod.DRUM_BLOCK_BASE + mod.DRUM_BLOCK_STRIDE * drum] = 0
            emu.dsp[mod.SAMPLE_DESCRIPTOR_TABLE] = 0x400000
            emu.run()
            raw = emu.dsp.get(mod.SAMPLE_RAW_BASE + drum)
            disp = emu.dsp.get(mod.SAMPLE_DISPLACEMENT_BASE + drum)
            if raw is None or disp is None:
                raise ValueError(f"sample drum {drum} value {value} did not write both words")
            if (raw >> 24) & 0x7F != (value & 0x7F):
                raise ValueError(f"sample drum {drum} raw word wrong for {value}")
    results["sample_start"] = "4 drums, raw X:$1E59.. and displacement X:$1E5D.. both written"

    # 6. Parameter ids outside the three hidden lanes must be ignored entirely.
    for parameter_id in (0, 1, 2, 6, 7, 8, 9, 27):
        emu = DecoderEmulator(patched, automation_byte=0x80 | 5, parameter_id=parameter_id)
        emu.run()
        if emu.setter_calls:
            raise ValueError(f"tagged value on unrelated parameter {parameter_id} wrote DSP state")
    results["unrelated_parameters_ignored"] = 8
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stock_sysex",
        type=Path,
        nargs="?",
        default=ROOT / "circuit-firmware-3592.syx",
        help="legitimate stock Circuit 1.8 build 3592 update SysEx",
    )
    args = parser.parse_args()

    stock, messages, original = load(args.stock_sysex)
    base, _ = release.build_image(stock)
    if mod.sha256(base) != V042_IMAGE:
        raise SystemExit("v0.4.2 base does not match the verified image")

    lfo_image, _ = mod.apply_clean_filter_lfo(base)
    patched, _ = mod.apply_shift_automation(lfo_image)

    report: dict[str, object] = {
        "base_image_sha256": mod.sha256(base),
        "patched_image_sha256": mod.sha256(patched),
        "static": static_checks(base, patched),
        "emulation": emulation_checks(patched),
        "recorder": record_checks(patched),
    }

    sysex = encode_firmware(patched, messages)
    if len(sysex) != len(original):
        raise SystemExit("repacked SysEx size changed")
    decoded, decoded_messages = decode_firmware(sysex)
    if decoded != patched:
        raise SystemExit("re-decoded SysEx does not match the patched image")
    if any(any(b & 0x80 for b in m.payload) for m in decoded_messages):
        raise SystemExit("output SysEx contains a non-MIDI-safe payload byte")
    report["sysex_sha256"] = mod.sha256(sysex)
    report["sysex_round_trip"] = "re-decodes exactly to the patched image"

    report["not_verifiable_here"] = {
        "factory_session_corpus": (
            "no Circuit session files in this workspace; the 0x80-0xFE encoding "
            "assumption is unverified"
        ),
        "record_ordering": (
            "hardware established recorder-first ordering; the verifier covers "
            "the matching Filter wrapper prediction but cannot reproduce the "
            "device's event fan-out itself"
        ),
        "sample_descriptor_at_reload": (
            "the descriptor may not be populated during a pattern reload; the "
            "displacement maths assumes it is"
        ),
    }

    out = ROOT / "build" / "circuit-shift-automation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
