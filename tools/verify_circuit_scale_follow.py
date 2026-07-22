"""Verify the combined Circuit Scale Follow and Drum Distortion Select patch."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

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
    UC_ARM_REG_SP,
)

from build_circuit_scale_follow import (
    BASE,
    CODE_SECTIONS,
    DISTORTION_ARM_COMMON,
    DISTORTION_CAVE_END,
    DISTORTION_HOOKS,
    DSP_CALL_ADDRESS,
    DSP_TRAMPOLINE_ADDRESS,
    DSP_TRAMPOLINE_PC,
    EVENT_HOOK_ENTRY,
    IMAGE_SIZE,
    INITIALIZED_BIT,
    MAIN_HANDLER_ENTRY,
    MAIN_HANDLER_RESUME,
    MAPPED_PATTERN_PITCH_GETTER,
    MAPPED_PITCH_GETTER,
    MODE_BIT,
    PARAMETER_MAP,
    PARAMETER_MAP_LENGTH,
    PATCH_SITES,
    PATCHED_IMAGE_SHA256,
    SCALE_MASK,
    SCALES_LOGICAL_ID,
    SHIFT_ARMED_BIT,
    SHIFT_LOGICAL_ID,
    STATE_ROOT,
    STATE_SCALE_MODE,
    STOCK_BUTTON_PRESSED,
    STOCK_DRUM_DIRTY_FLAGS,
    STOCK_IMAGE_SHA256,
    STOCK_MAIN_VIEW_SELECT,
    STOCK_PATTERN_PARAMETER_GETTER,
    STOCK_DRUM_CONTROL_GETTER,
    STOCK_PARAMETER_FIELD_GET,
    STOCK_SCALE_MASKS,
    STOCK_SCALE_LIMIT_INIT,
    REFRESH_ALL_DRUM_PITCH,
    TOGGLE_SCALES_CASE,
    UPDATE_ROOT_ENTRY,
    UPDATE_SCALE,
    assemble,
    build_distortion_arm_code,
    build_distortion_dsp_trampoline,
    dsp_bytes,
    sha256,
)
from circuit_scale_follow import map_pitch


FLASH_MAP_BASE = 0x08000000
FLASH_MAP_SIZE = 0x00040000
RAM_BASE = 0x20000000
RAM_SIZE = 0x00020000
STACK = 0x2001F000
RETURN_SENTINEL = 0x08007000
STOCK_PARAMETER_OBJECT_CLOSE = 0x080145CE
STOCK_SCALE_SELECTION_GET = 0x080105F4
STOCK_MASTER_SCALE_ROOT_UPDATE = 0x0801C91C
STOCK_DSP_SETTER = 0x0801633C
STOCK_DRUM_DIRTY_DISPATCH = 0x0800CE1C
STOCK_DRUM_EVENT_HANDLER = 0x0801FF24
STOCK_DRUM_NOTE_TRIGGER = 0x08016694
STOCK_DRUM_CALLBACK_TABLE = 0x0802FF40
STOCK_DRUM_PARAMETER_DIRTY_MAP = 0x0802EC6C
STOCK_UI_SCALE_HANDLER = 0x08010606
STOCK_UI_ROOT_HANDLER = 0x08014F2C
STOCK_UI_ROOT_DIRECT_HANDLER = 0x08014F68
LIVE_DRUM_PITCH_CALLBACKS = (
    (0x08018AFA, 0x00A0),
    (0x0801A154, 0x00AA),
    (0x08019EF4, 0x00B4),
    (0x08019E5C, 0x00BE),
)
DRUM_PITCH_PARAMETERS = (
    (2, 0x00A0),
    (9, 0x00AA),
    (16, 0x00B4),
    (23, 0x00BE),
)
EXPECTED_DRUM_GETTERS = [
    (0x10, index) for index, _ in DRUM_PITCH_PARAMETERS
]
EXPECTED_DRUM_CALLBACKS = (
    0x08018AFB,
    0x0801A155,
    0x08019EF5,
    0x08019E5D,
)
DRUM_NOTE_EVENTS = (
    (0, 0x3C),
    (1, 0x3E),
    (2, 0x40),
    (3, 0x41),
)
MODE_ON_STATE = MODE_BIT | INITIALIZED_BIT
MODE_OFF_STATE = INITIALIZED_BIT


def read_word(data: bytes, address: int) -> int:
    return struct.unpack_from("<I", data, address - BASE)[0]


def stock_pitch_dsp_value(pitch: int) -> int:
    delta = pitch - 64
    half_delta = abs(delta) // 2
    if delta < 0:
        half_delta = -half_delta
    return (((pitch + 64 + half_delta) * 2) & 0xFFFF) << 8


def expected_refresh_writes(
    raw_pitch: int,
    scale: int,
    root: int,
    *,
    enabled: bool,
) -> list[tuple[int, int]]:
    pitch = map_pitch(raw_pitch, scale, root % 12) if enabled else raw_pitch
    value = stock_pitch_dsp_value(pitch)
    return [(value, register) for _, register in DRUM_PITCH_PARAMETERS]


def enter_panel_event(emulator: "Emulator", logical_id: int) -> None:
    """Run the patched entry hook through the first untouched stock opcode."""
    emulator.run(
        MAIN_HANDLER_ENTRY,
        r0=0x20003000,
        r1=logical_id,
        r2=0xA5A5A5A5,
        r3=0x5A5A5A5A,
        stop_at=MAIN_HANDLER_RESUME,
        max_instructions=200,
    )


def distortion_static_checks(patched: bytes, manifest: dict) -> dict:
    dsp_code, dsp_metadata = build_distortion_dsp_trampoline()
    start = DSP_TRAMPOLINE_ADDRESS - BASE
    if patched[start : start + len(dsp_code)] != dsp_code:
        raise ValueError("distortion DSP trampoline does not match the verified build")

    entries, common = build_distortion_arm_code()
    for address, code, purpose in entries:
        start = address - BASE
        if patched[start : start + len(code)] != code:
            raise ValueError(f"{purpose} does not match the verified build")
    common_start = DISTORTION_ARM_COMMON - BASE
    if patched[common_start : common_start + len(common)] != common:
        raise ValueError("shared distortion ARM wrapper does not match the verified build")
    if DISTORTION_ARM_COMMON + len(common) > DISTORTION_CAVE_END:
        raise ValueError("shared distortion ARM wrapper exceeds its verified cave")

    redirect = dsp_bytes([0x0AF080, DSP_TRAMPOLINE_PC, 0x000000])
    redirect_start = DSP_CALL_ADDRESS - BASE
    if patched[redirect_start : redirect_start + len(redirect)] != redirect:
        raise ValueError("stock DSP distortion call was not redirected correctly")

    for hook in DISTORTION_HOOKS:
        expected = assemble(f"bl {hook.target:#x}", hook.address)
        start = hook.address - BASE
        if patched[start : start + len(expected)] != expected:
            raise ValueError(f"Drum {hook.drum} distortion hook is incorrect")

    details = manifest.get("distortion_selector", {})
    if details.get("dsp_trampoline_words") != dsp_metadata["word_count"]:
        raise ValueError("manifest distortion trampoline length is incorrect")
    if details.get("drum_3_4_storage") != "packed fields in proven-stable DSP Y:$010B":
        raise ValueError("manifest does not declare the hardware-tested Drum 3/4 storage")

    return {
        "dsp_trampoline_words": dsp_metadata["word_count"],
        "arm_wrapper_bytes": len(common),
        "drum_hooks": len(DISTORTION_HOOKS),
        "drum_3_4_state": "packed in DSP Y:$010B",
    }


def static_checks(stock: bytes, patched: bytes, manifest: dict) -> dict:
    if len(stock) != IMAGE_SIZE or len(patched) != IMAGE_SIZE:
        raise ValueError("unexpected image size")
    if sha256(stock) != STOCK_IMAGE_SHA256:
        raise ValueError("stock image hash mismatch")
    if sha256(patched) != manifest["patched_image_sha256"]:
        raise ValueError("patched image hash mismatch")
    if sha256(patched) != PATCHED_IMAGE_SHA256:
        raise ValueError("patched image is not the hardware-tested combined release")

    distortion_results = distortion_static_checks(patched, manifest)

    disassembler = Cs(
        CS_ARCH_ARM,
        CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN,
    )

    section_results = []
    lengths = {
        int(section["address"], 16): section["length"]
        for section in manifest["code_sections"]
        if section["purpose"] != "notes-per-octave table for the 16 stock scales"
    }
    for start, _, _, purpose in CODE_SECTIONS:
        length = lengths[start]
        code = patched[start - BASE : start - BASE + length]
        instructions = list(disassembler.disasm(code, start))
        decoded_length = sum(instruction.size for instruction in instructions)
        if decoded_length != length:
            raise ValueError(
                f"section at {start:#010x} decoded {decoded_length}/{length} bytes"
            )
        section_results.append(
            {
                "address": f"{start:#010x}",
                "length": length,
                "instruction_count": len(instructions),
                "purpose": purpose,
            }
        )

    site_results = []
    for site in PATCH_SITES:
        code = patched[site.address - BASE : site.address - BASE + len(site.stock)]
        instructions = list(disassembler.disasm(code, site.address))
        if len(instructions) != 1 or instructions[0].size != 4:
            raise ValueError(f"patch site at {site.address:#010x} is not one 32-bit branch")
        site_results.append(
            {
                "address": f"{site.address:#010x}",
                "instruction": f"{instructions[0].mnemonic} {instructions[0].op_str}",
                "purpose": site.purpose,
            }
        )

    if STATE_SCALE_MODE != PARAMETER_MAP + PARAMETER_MAP_LENGTH:
        raise ValueError("scale/mode state is not immediately after the parameter map")
    if STATE_ROOT != STATE_SCALE_MODE + 1 or STATE_ROOT + 1 != 0x20002DB0:
        raise ValueError("state bytes do not exactly occupy the two-byte alignment gap")
    if MODE_BIT & SHIFT_ARMED_BIT or MODE_BIT & INITIALIZED_BIT:
        raise ValueError("mode bit overlaps another state flag")
    if SHIFT_ARMED_BIT & INITIALIZED_BIT:
        raise ValueError("Shift and initialization flags overlap")
    if (MODE_BIT | SHIFT_ARMED_BIT | INITIALIZED_BIT) & SCALE_MASK:
        raise ValueError("state flags overlap the scale-index field")
    if manifest.get("mode_default") != "on after one-time lazy RAM initialization":
        raise ValueError("manifest does not declare the verified default-on behavior")
    if manifest.get("pitch_paths") != (
        "four stock pattern/session reload reads plus four live Drum "
        "parameter callbacks"
    ):
        raise ValueError("manifest does not declare both verified Drum Pitch paths")
    if stock[0x080094E4 - BASE : 0x080094E8 - BASE] != bytes.fromhex("ae 2b d3 d1"):
        raise ValueError("parameter-map initialization bound is not the verified 0xAE")
    for address in (
        0x0800AEBC,
        0x0800D8E4,
        0x0801C95C,
    ):
        start = address - BASE
        if patched[start : start + 4] != stock[start : start + 4]:
            raise ValueError(f"stock callback handling changed at {address:#010x}")

    expected_dirty_map = bytes(
        [0x01] * 7 + [0x02] * 7 + [0x04] * 7 + [0x08] * 7
    )
    dirty_map_start = STOCK_DRUM_PARAMETER_DIRTY_MAP - BASE
    if stock[dirty_map_start : dirty_map_start + 28] != expected_dirty_map:
        raise ValueError("stock Drum parameter-to-dirty-bit map changed")
    if patched[dirty_map_start : dirty_map_start + 28] != expected_dirty_map:
        raise ValueError("patch changed the stock Drum dirty-bit map")
    callback_start = STOCK_DRUM_CALLBACK_TABLE - BASE
    callbacks = struct.unpack_from("<4I", stock, callback_start)
    if callbacks != EXPECTED_DRUM_CALLBACKS:
        raise ValueError(f"stock Drum callback table changed: {callbacks}")
    if patched[callback_start : callback_start + 16] != stock[
        callback_start : callback_start + 16
    ]:
        raise ValueError("patch changed the stock Drum callback table")
    dispatcher_start = STOCK_DRUM_DIRTY_DISPATCH - BASE
    if patched[dispatcher_start : dispatcher_start + 0x20] != stock[
        dispatcher_start : dispatcher_start + 0x20
    ]:
        raise ValueError("patch changed the stock Drum dirty-bit dispatcher")
    event_start = STOCK_DRUM_EVENT_HANDLER - BASE
    if patched[event_start : event_start + 0x50] != stock[event_start : event_start + 0x50]:
        raise ValueError("patch changed the stock Drum note-event handler")
    tbb_table_start = 0x0801FF3E - BASE
    if stock[tbb_table_start : tbb_table_start + 6] != bytes((3, 0x77, 8, 0x77, 0x11, 0x16)):
        raise ValueError("stock Drum note-event dispatch table changed")

    return {
        "sections": section_results,
        "sites": site_results,
        "state_padding": {
            "parameter_map": f"{PARAMETER_MAP:#010x}",
            "parameter_map_length": PARAMETER_MAP_LENGTH,
            "scale_mode_byte": f"{STATE_SCALE_MODE:#010x}",
            "root_byte": f"{STATE_ROOT:#010x}",
            "next_stock_global": "0x20002db0",
        },
        "stock_callback_tables": "unchanged",
        "distortion_selector": distortion_results,
        "pitch_read_hooks": "four reload reads and four live callbacks redirected",
        "stock_drum_scheduler": (
            "verified parameter map, callback pointers, dirty dispatcher, and note-event consumer"
        ),
    }


class Emulator:
    def __init__(self, image: bytes):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(FLASH_MAP_BASE, FLASH_MAP_SIZE)
        self.uc.mem_write(BASE, image)
        self.uc.mem_map(RAM_BASE, RAM_SIZE)
        self.raw_pitch = 64
        self.shift_pressed = False
        self.pressed_button_ids: set[int] | None = None
        self.button_pressed_calls: list[int] = []
        self.view_select_calls: list[tuple[int, int, int]] = []
        self.drum_getter_calls: list[tuple[int, int]] = []
        self.pattern_getter_calls: list[tuple[int, int]] = []
        self.dsp_calls: list[tuple[int, int]] = []
        self.drum_note_triggers: list[tuple[int, int]] = []
        self.scale_index_address = 0x20005040
        self.root_field_address = 0x20005060
        self.parameter_close_calls: list[int] = []
        self.stop_at = RETURN_SENTINEL
        self.uc.hook_add(UC_HOOK_CODE, self._hook)

    def _hook(self, uc: Uc, address: int, size: int, _: object) -> None:
        if address == self.stop_at:
            uc.emu_stop()
            return
        if address == STOCK_DRUM_CONTROL_GETTER:
            self.drum_getter_calls.append(
                (
                    uc.reg_read(UC_ARM_REG_R0),
                    uc.reg_read(UC_ARM_REG_R1),
                )
            )
            uc.reg_write(UC_ARM_REG_R0, self.raw_pitch)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        if address == STOCK_PATTERN_PARAMETER_GETTER:
            self.pattern_getter_calls.append(
                (
                    uc.reg_read(UC_ARM_REG_R0),
                    uc.reg_read(UC_ARM_REG_R1),
                )
            )
            uc.reg_write(UC_ARM_REG_R0, self.raw_pitch)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        if address == STOCK_DSP_SETTER:
            self.dsp_calls.append(
                (
                    uc.reg_read(UC_ARM_REG_R0),
                    uc.reg_read(UC_ARM_REG_R1),
                )
            )
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        if address == STOCK_DRUM_NOTE_TRIGGER:
            self.drum_note_triggers.append(
                (
                    uc.reg_read(UC_ARM_REG_R0),
                    uc.reg_read(UC_ARM_REG_R1),
                )
            )
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        if address == STOCK_PARAMETER_OBJECT_CLOSE:
            self.parameter_close_calls.append(uc.reg_read(UC_ARM_REG_R0))
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        if address == STOCK_PARAMETER_FIELD_GET:
            field = uc.reg_read(UC_ARM_REG_R1)
            if field == 0:
                result = self.root_field_address
            elif field == 1:
                result = self.scale_index_address
            else:
                raise ValueError(f"unexpected parameter field index {field}")
            uc.reg_write(UC_ARM_REG_R0, result)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        if address == STOCK_SCALE_SELECTION_GET:
            uc.reg_write(UC_ARM_REG_R0, self.scale_index_address)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        if address == STOCK_BUTTON_PRESSED:
            logical_id = uc.reg_read(UC_ARM_REG_R0)
            self.button_pressed_calls.append(logical_id)
            pressed = (
                self.shift_pressed
                if self.pressed_button_ids is None
                else logical_id in self.pressed_button_ids
            )
            uc.reg_write(UC_ARM_REG_R0, int(pressed))
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        if address == STOCK_MAIN_VIEW_SELECT:
            self.view_select_calls.append(
                (
                    uc.reg_read(UC_ARM_REG_R0),
                    uc.reg_read(UC_ARM_REG_R1),
                    uc.reg_read(UC_ARM_REG_R2),
                )
            )
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
    def write_word(self, address: int, value: int) -> None:
        self.uc.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def read_word(self, address: int) -> int:
        return struct.unpack("<I", self.uc.mem_read(address, 4))[0]

    def write_byte(self, address: int, value: int) -> None:
        self.uc.mem_write(address, bytes((value & 0xFF,)))

    def read_byte(self, address: int) -> int:
        return self.uc.mem_read(address, 1)[0]

    def run(
        self,
        start: int,
        *,
        r0: int = 0,
        r1: int = 0,
        r2: int = 0,
        r3: int = 0,
        r4: int = 0,
        r5: int = 0,
        stack_words: tuple[int, ...] = (),
        stop_at: int = RETURN_SENTINEL,
        max_instructions: int = 2000,
    ) -> int:
        self.stop_at = stop_at
        self.uc.reg_write(UC_ARM_REG_R0, r0)
        self.uc.reg_write(UC_ARM_REG_R1, r1)
        self.uc.reg_write(UC_ARM_REG_R2, r2)
        self.uc.reg_write(UC_ARM_REG_R3, r3)
        self.uc.reg_write(UC_ARM_REG_R4, r4)
        self.uc.reg_write(UC_ARM_REG_R5, r5)
        self.uc.reg_write(UC_ARM_REG_SP, STACK)
        self.uc.reg_write(UC_ARM_REG_LR, RETURN_SENTINEL | 1)
        if stack_words:
            self.uc.mem_write(
                STACK,
                b"".join(struct.pack("<I", word & 0xFFFFFFFF) for word in stack_words),
            )
        self.uc.emu_start(start | 1, stop_at | 1, count=max_instructions)
        pc = self.uc.reg_read(UC_ARM_REG_PC) & ~1
        if pc != stop_at:
            raise ValueError(
                f"emulation from {start:#010x} stopped at {pc:#010x}, "
                f"expected {stop_at:#010x}"
            )
        return self.uc.reg_read(UC_ARM_REG_R0)


def configure_held_scales_ui(emulator: Emulator) -> tuple[int, int]:
    child = 0x20007000
    selection = 0x20007100
    emulator.write_word(child + 0x0C, selection)
    emulator.write_word(selection + 0x04, 0)
    emulator.write_word(selection + 0x0C, emulator.root_field_address)
    emulator.write_byte(emulator.scale_index_address, 1)
    emulator.write_byte(emulator.root_field_address, 0)
    return child, selection


def assert_refresh(
    emulator: Emulator,
    raw_pitch: int,
    scale: int,
    root: int,
    *,
    enabled: bool,
    label: str,
) -> None:
    dirty_before = emulator.read_byte(STOCK_DRUM_DIRTY_FLAGS)
    if dirty_before & 0x0F != 0x0F:
        raise ValueError(
            f"{label} did not request all four stock Drum callbacks: "
            f"dirty={dirty_before:#04x}"
        )
    trigger_start = len(emulator.drum_note_triggers)
    velocity = 0x64
    for bit, event in DRUM_NOTE_EVENTS:
        emulator.run(
            STOCK_DRUM_EVENT_HANDLER,
            r1=0x10,
            r2=event,
            r3=velocity,
            max_instructions=20000,
        )
    actual_triggers = emulator.drum_note_triggers[trigger_start:]
    expected_triggers = [(bit, velocity) for bit, _ in DRUM_NOTE_EVENTS]
    if actual_triggers != expected_triggers:
        raise ValueError(
            f"{label} did not continue through the four stock Drum triggers: "
            f"expected={expected_triggers}, got={actual_triggers}"
        )
    dirty_after = emulator.read_byte(STOCK_DRUM_DIRTY_FLAGS)
    if dirty_after != (dirty_before & 0xF0):
        raise ValueError(
            f"{label} did not consume exactly the four Drum dirty bits: "
            f"before={dirty_before:#04x}, after={dirty_after:#04x}"
        )
    pitch_getters = [
        call for call in emulator.drum_getter_calls if call in EXPECTED_DRUM_GETTERS
    ]
    if pitch_getters != EXPECTED_DRUM_GETTERS:
        raise ValueError(
            f"{label} read the wrong drum parameters: "
            f"{pitch_getters}"
        )
    expected = expected_refresh_writes(
        raw_pitch,
        scale,
        root,
        enabled=enabled,
    )
    pitch_registers = {register for _, register in DRUM_PITCH_PARAMETERS}
    pitch_writes = [
        (value, register)
        for value, register in emulator.dsp_calls
        if register in pitch_registers
    ]
    if pitch_writes != expected:
        raise ValueError(
            f"{label} DSP writes differ: expected={expected}, "
            f"got={pitch_writes}"
        )


def held_scales_ui_checks(stock: bytes, patched: bytes) -> dict:
    # Mode-off behavior must remain stock for the two exact pad handlers used
    # by the momentary Scales overlay.
    stock_scale = Emulator(stock)
    patched_scale = Emulator(patched)
    stock_child, stock_selection = configure_held_scales_ui(stock_scale)
    patched_child, patched_selection = configure_held_scales_ui(patched_scale)
    patched_scale.write_byte(STATE_SCALE_MODE, MODE_OFF_STATE | 1)
    stock_result = stock_scale.run(
        STOCK_UI_SCALE_HANDLER,
        r0=stock_child,
        r1=14,
        max_instructions=10000,
    )
    patched_result = patched_scale.run(
        STOCK_UI_SCALE_HANDLER,
        r0=patched_child,
        r1=14,
        max_instructions=10000,
    )
    if patched_result != stock_result:
        raise ValueError("mode-off held-Scales scale handler changed r0")
    if stock_scale.read_byte(stock_scale.scale_index_address) != 14:
        raise ValueError("stock held-Scales scale handler did not commit scale 14")
    if patched_scale.read_byte(patched_scale.scale_index_address) != 14:
        raise ValueError("patched held-Scales scale handler did not commit scale 14")
    if bytes(stock_scale.uc.mem_read(stock_child, 0x20)) != bytes(
        patched_scale.uc.mem_read(patched_child, 0x20)
    ):
        raise ValueError("mode-off scale handler changed the child object")
    if bytes(stock_scale.uc.mem_read(stock_selection, 0x20)) != bytes(
        patched_scale.uc.mem_read(patched_selection, 0x20)
    ):
        raise ValueError("mode-off scale handler changed the selection object")
    if patched_scale.parameter_close_calls != [patched_selection]:
        raise ValueError("patched scale handler did not perform one stock commit")
    if (
        patched_scale.drum_getter_calls
        or patched_scale.dsp_calls
        or patched_scale.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
    ):
        raise ValueError("mode-off held-Scales scale handler refreshed drum pitch")

    stock_root = Emulator(stock)
    patched_root = Emulator(patched)
    stock_child, stock_selection = configure_held_scales_ui(stock_root)
    patched_child, patched_selection = configure_held_scales_ui(patched_root)
    patched_root.write_byte(STATE_SCALE_MODE, MODE_OFF_STATE | 1)
    stock_result = stock_root.run(
        STOCK_UI_ROOT_HANDLER,
        r0=stock_child,
        r1=12,
        max_instructions=10000,
    )
    patched_result = patched_root.run(
        STOCK_UI_ROOT_HANDLER,
        r0=patched_child,
        r1=12,
        max_instructions=10000,
    )
    if patched_result != stock_result:
        raise ValueError("mode-off held-Scales root handler changed r0")
    if stock_root.read_byte(stock_root.root_field_address) != 7:
        raise ValueError("stock held-Scales root handler did not map pad 12 to G")
    if patched_root.read_byte(patched_root.root_field_address) != 7:
        raise ValueError("patched held-Scales root handler did not map pad 12 to G")
    if bytes(stock_root.uc.mem_read(stock_child, 0x20)) != bytes(
        patched_root.uc.mem_read(patched_child, 0x20)
    ):
        raise ValueError("mode-off root handler changed the child object")
    if bytes(stock_root.uc.mem_read(stock_selection, 0x20)) != bytes(
        patched_root.uc.mem_read(patched_selection, 0x20)
    ):
        raise ValueError("mode-off root handler changed the selection object")
    if patched_root.parameter_close_calls != [patched_selection]:
        raise ValueError("patched root handler did not perform one stock commit")
    if (
        patched_root.drum_getter_calls
        or patched_root.dsp_calls
        or patched_root.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
    ):
        raise ValueError("mode-off held-Scales root handler refreshed drum pitch")

    # Mode-on scale selection must reach the final post-commit refresh.
    scale_emulator = Emulator(patched)
    child, selection = configure_held_scales_ui(scale_emulator)
    scale_emulator.raw_pitch = 72
    scale_emulator.write_byte(STATE_SCALE_MODE, MODE_ON_STATE | 1)
    scale_emulator.write_byte(STATE_ROOT, 0)
    scale_emulator.run(
        STOCK_UI_SCALE_HANDLER,
        r0=child,
        r1=14,
        max_instructions=10000,
    )
    if scale_emulator.parameter_close_calls != [selection]:
        raise ValueError("mode-on scale event did not complete the stock commit")
    if scale_emulator.read_byte(STATE_SCALE_MODE) != (MODE_ON_STATE | 14):
        raise ValueError("real held-Scales scale event did not capture scale 14")
    assert_refresh(
        scale_emulator,
        72,
        14,
        0,
        enabled=True,
        label="real held-Scales scale event",
    )

    # Mode-on root selection must map the physical root pad, commit it, then
    # reapply all drum pitches.  Exercise the exact C -> G user scenario.
    root_emulator = Emulator(patched)
    child, _ = configure_held_scales_ui(root_emulator)
    root_emulator.raw_pitch = 72
    root_emulator.write_byte(root_emulator.scale_index_address, 1)
    root_emulator.write_byte(STATE_SCALE_MODE, MODE_ON_STATE | 1)
    root_emulator.write_byte(STATE_ROOT, 7)
    root_emulator.write_byte(root_emulator.root_field_address, 7)
    root_emulator.run(
        STOCK_UI_ROOT_HANDLER,
        r0=child,
        r1=8,
        max_instructions=10000,
    )
    if root_emulator.read_byte(root_emulator.root_field_address) != 0:
        raise ValueError("held-Scales C pad did not commit root C")
    if root_emulator.read_byte(STATE_ROOT) != 0:
        raise ValueError("held-Scales C pad did not capture root C")
    assert_refresh(
        root_emulator,
        72,
        1,
        0,
        enabled=True,
        label="held-Scales C root event",
    )
    c_writes = list(root_emulator.dsp_calls)

    root_emulator.drum_getter_calls.clear()
    root_emulator.dsp_calls.clear()
    root_emulator.run(
        STOCK_UI_ROOT_HANDLER,
        r0=child,
        r1=12,
        max_instructions=10000,
    )
    if root_emulator.read_byte(root_emulator.root_field_address) != 7:
        raise ValueError("held-Scales G pad did not commit root G")
    if root_emulator.read_byte(STATE_ROOT) != 7:
        raise ValueError("held-Scales G pad did not capture root G")
    assert_refresh(
        root_emulator,
        72,
        1,
        7,
        enabled=True,
        label="held-Scales G root event",
    )
    if root_emulator.dsp_calls == c_writes:
        raise ValueError("full held-Scales C -> G path produced identical DSP writes")

    # The firmware registers a second root setter in a separate callback
    # table.  Hardware can use this direct-value path instead of the mapped
    # physical-pad handler above, so its independent tail must be hooked too.
    direct_root = Emulator(patched)
    child, selection = configure_held_scales_ui(direct_root)
    direct_root.raw_pitch = 72
    direct_root.write_byte(direct_root.scale_index_address, 1)
    direct_root.write_byte(STATE_SCALE_MODE, MODE_ON_STATE | 1)
    direct_root.write_byte(STATE_ROOT, 0)
    direct_root.write_byte(direct_root.root_field_address, 0)
    direct_root.run(
        STOCK_UI_ROOT_DIRECT_HANDLER,
        r0=child,
        r1=7,
        max_instructions=10000,
    )
    if direct_root.parameter_close_calls != [selection]:
        raise ValueError("direct root setter did not complete the stock commit")
    if direct_root.read_byte(direct_root.root_field_address) != 7:
        raise ValueError("direct root setter did not commit G")
    if direct_root.read_byte(STATE_ROOT) != 7:
        raise ValueError("direct root setter did not capture G")
    assert_refresh(
        direct_root,
        72,
        1,
        7,
        enabled=True,
        label="direct root G event",
    )

    # At CC72, stock scale indices 1 and 0 are Major and Natural Minor and
    # must produce E and Eb respectively at root C.
    scale_emulator = Emulator(patched)
    child, _ = configure_held_scales_ui(scale_emulator)
    scale_emulator.raw_pitch = 72
    scale_emulator.write_byte(STATE_SCALE_MODE, MODE_ON_STATE | 14)
    scale_emulator.write_byte(STATE_ROOT, 0)
    scale_emulator.run(
        STOCK_UI_SCALE_HANDLER,
        r0=child,
        r1=1,
        max_instructions=10000,
    )
    major_writes = list(scale_emulator.dsp_calls)
    scale_emulator.drum_getter_calls.clear()
    scale_emulator.dsp_calls.clear()
    scale_emulator.run(
        STOCK_UI_SCALE_HANDLER,
        r0=child,
        r1=0,
        max_instructions=10000,
    )
    assert_refresh(
        scale_emulator,
        72,
        0,
        0,
        enabled=True,
        label="held-Scales Natural Minor event",
    )
    if scale_emulator.dsp_calls == major_writes:
        raise ValueError("full held-Scales Major -> Minor path produced identical DSP writes")

    # Exhaust every selectable scale and every physical root pad in both mode
    # states.  This proves the hooks are not tailored only to C/G or Major/Minor.
    exhaustive_scale_events = 0
    for enabled in (False, True):
        for selected_scale in range(16):
            test = Emulator(patched)
            child, _ = configure_held_scales_ui(test)
            initial_scale = (selected_scale + 1) % 16
            test.write_byte(test.scale_index_address, initial_scale)
            test.write_byte(
                STATE_SCALE_MODE,
                (MODE_ON_STATE if enabled else MODE_OFF_STATE) | initial_scale,
            )
            test.write_byte(STATE_ROOT, 3)
            test.raw_pitch = 72
            test.run(
                STOCK_UI_SCALE_HANDLER,
                r0=child,
                r1=selected_scale,
                max_instructions=10000,
            )
            expected_state = (
                MODE_ON_STATE if enabled else MODE_OFF_STATE
            ) | selected_scale
            if test.read_byte(STATE_SCALE_MODE) != expected_state:
                raise ValueError(
                    f"scale pad {selected_scale} produced state "
                    f"{test.read_byte(STATE_SCALE_MODE):#04x}"
                )
            if enabled:
                assert_refresh(
                    test,
                    72,
                    selected_scale,
                    3,
                    enabled=True,
                    label=f"exhaustive scale pad {selected_scale}",
                )
            elif (
                test.drum_getter_calls
                or test.dsp_calls
                or test.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
            ):
                raise ValueError(
                    f"mode-off scale pad {selected_scale} refreshed drum pitch"
                )
            exhaustive_scale_events += 1

    root_pad_map = {
        1: 1,
        2: 3,
        4: 6,
        5: 8,
        6: 10,
        8: 0,
        9: 2,
        10: 4,
        11: 5,
        12: 7,
        13: 9,
        14: 11,
    }
    exhaustive_root_events = 0
    for enabled in (False, True):
        for pad, selected_root in root_pad_map.items():
            test = Emulator(patched)
            child, _ = configure_held_scales_ui(test)
            initial_root = (selected_root + 1) % 12
            test.write_byte(test.root_field_address, initial_root)
            test.write_byte(
                STATE_SCALE_MODE,
                (MODE_ON_STATE if enabled else MODE_OFF_STATE) | 1,
            )
            test.write_byte(STATE_ROOT, initial_root)
            test.raw_pitch = 72
            test.run(
                STOCK_UI_ROOT_HANDLER,
                r0=child,
                r1=pad,
                max_instructions=10000,
            )
            if test.read_byte(test.root_field_address) != selected_root:
                raise ValueError(f"root pad {pad} did not commit {selected_root}")
            if test.read_byte(STATE_ROOT) != selected_root:
                raise ValueError(f"root pad {pad} did not capture {selected_root}")
            if enabled:
                assert_refresh(
                    test,
                    72,
                    1,
                    selected_root,
                    enabled=True,
                    label=f"exhaustive root pad {pad}",
                )
            elif (
                test.drum_getter_calls
                or test.dsp_calls
                or test.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
            ):
                raise ValueError(f"mode-off root pad {pad} refreshed drum pitch")
            exhaustive_root_events += 1

    ignored_events = 0
    for pad in (0, 3, 7, 15):
        test = Emulator(patched)
        child, _ = configure_held_scales_ui(test)
        test.write_byte(STATE_SCALE_MODE, MODE_ON_STATE | 1)
        test.write_byte(STATE_ROOT, 0)
        test.run(
            STOCK_UI_ROOT_HANDLER,
            r0=child,
            r1=pad,
            max_instructions=10000,
        )
        if (
            test.parameter_close_calls
            or test.drum_getter_calls
            or test.dsp_calls
            or test.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
        ):
            raise ValueError(f"invalid root pad {pad} was not ignored")
        ignored_events += 1

    for handler, selected in (
        (STOCK_UI_SCALE_HANDLER, 1),
        (STOCK_UI_ROOT_HANDLER, 8),
    ):
        test = Emulator(patched)
        child, _ = configure_held_scales_ui(test)
        test.write_byte(STATE_SCALE_MODE, MODE_ON_STATE | 1)
        test.write_byte(STATE_ROOT, 0)
        test.run(handler, r0=child, r1=selected, max_instructions=10000)
        if (
            test.parameter_close_calls
            or test.drum_getter_calls
            or test.dsp_calls
            or test.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
        ):
            raise ValueError(
                f"unchanged held-Scales selection at {handler:#010x} was not ignored"
            )
        ignored_events += 1

    return {
        "held_scales_mode_off_handlers": "bit-for-bit stock return and committed values",
        "held_scales_scale_commit_path": (
            "requested all four callbacks and consumed them on real Drum note events"
        ),
        "held_scales_root_commit_path": (
            "requested all four callbacks and consumed them on real Drum note events"
        ),
        "direct_root_commit_path": (
            "captured the independently registered direct root setter"
        ),
        "held_scales_c_to_g": "produced distinct DSP writes on the next Drum hits",
        "held_scales_major_to_minor": (
            "produced distinct DSP writes on the next Drum hits"
        ),
        "held_scales_exhaustive_scale_events": exhaustive_scale_events,
        "held_scales_exhaustive_root_events": exhaustive_root_events,
        "held_scales_ignored_events": ignored_events,
    }


def emulation_checks(stock: bytes, patched: bytes) -> dict:
    emulator = Emulator(patched)
    stock_emulator = Emulator(stock)

    # Run the complete patched stock master scale/root update, not only the
    # injected tails.  This checks the real caller's registers and stack.
    master = 0x20004000
    selection = 0x20005000
    scale_output = 0x20006000
    root_output = 0x20006020
    root_source = 0x20005060
    scale = 14
    root_note = 23
    for target in (stock_emulator, emulator):
        target.write_word(master + 0x58, selection)
        target.write_word(master + 0x5C, 0x20006100)
        target.write_word(master + 0x08, scale_output)
        target.write_word(master + 0x0C, root_output)
        target.write_word(selection + 0x04, 0)
        target.write_word(selection + 0x0C, root_source)
        target.write_byte(target.scale_index_address, scale)
        target.write_byte(root_source, root_note)
        target.write_byte(STATE_SCALE_MODE, 0)
        target.write_byte(STATE_ROOT, 0)
    stock_result = stock_emulator.run(
        STOCK_MASTER_SCALE_ROOT_UPDATE,
        r0=master,
        r2=3,
        max_instructions=10000,
    )
    patched_result = emulator.run(
        STOCK_MASTER_SCALE_ROOT_UPDATE,
        r0=master,
        r2=3,
        max_instructions=10000,
    )
    if patched_result != stock_result:
        raise ValueError(
            "complete master update changed the stock r0 return contract: "
            f"stock={stock_result:#010x}, patched={patched_result:#010x}"
        )
    if emulator.read_byte(STATE_SCALE_MODE) != scale:
        raise ValueError("complete master update did not mirror the scale")
    if emulator.read_byte(STATE_ROOT) != root_note:
        raise ValueError("complete master update did not capture the raw root")
    if emulator.read_byte(root_output) != root_note:
        raise ValueError("complete master update changed the stock root write")
    expected_mask = emulator.read_word(STOCK_SCALE_MASKS + scale * 4)
    if emulator.read_word(scale_output + 4) != expected_mask:
        raise ValueError("complete master update changed the stock scale-mask write")
    if (
        emulator.drum_getter_calls
        or emulator.dsp_calls
        or emulator.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
    ):
        raise ValueError("mode-off master update unexpectedly refreshed drum pitch")

    # The callback may run with no scale/root change flags during construction
    # or session reload.  Its entry wrapper must still capture the authoritative
    # stock Scale and Root values while preserving all patch-owned flags.
    capture = Emulator(patched)
    capture_master = 0x20008000
    capture_selection = 0x20008100
    capture_root_source = 0x20008200
    capture.write_word(capture_master + 0x58, capture_selection)
    capture.write_word(capture_master + 0x5C, 0x20008300)
    capture.write_word(capture_selection + 0x04, 0)
    capture.write_word(capture_selection + 0x0C, capture_root_source)
    capture.write_byte(capture.scale_index_address, 8)
    capture.write_byte(capture_root_source, 11)
    capture.write_byte(
        STATE_SCALE_MODE,
        MODE_OFF_STATE | SHIFT_ARMED_BIT | 1,
    )
    capture.write_byte(STATE_ROOT, 3)
    capture.run(
        STOCK_MASTER_SCALE_ROOT_UPDATE,
        r0=capture_master,
        r2=0,
        max_instructions=10000,
    )
    if capture.read_byte(STATE_SCALE_MODE) != (
        MODE_OFF_STATE | SHIFT_ARMED_BIT | 8
    ):
        raise ValueError("flagless master callback did not capture stock Scale")
    if capture.read_byte(STATE_ROOT) != 11:
        raise ValueError("flagless master callback did not capture stock Root")
    if (
        capture.drum_getter_calls
        or capture.dsp_calls
        or capture.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
    ):
        raise ValueError("mode-off authoritative capture refreshed drum pitch")

    # Reproduce the hardware reboot case directly.  RAM begins at zero; the
    # first pitch read must initialize Scale Follow ON and apply the startup
    # Natural Minor/C mapping even if no panel button has been pressed.
    cold_getter = Emulator(patched)
    cold_getter.raw_pitch = 72
    cold_pitch = cold_getter.run(
        MAPPED_PITCH_GETTER,
        r0=0x10,
        r1=2,
        max_instructions=1000,
    )
    if cold_getter.read_byte(STATE_SCALE_MODE) != MODE_ON_STATE:
        raise ValueError("cold first Drum Pitch read did not initialize mode ON")
    if cold_pitch != map_pitch(72, 0, 0):
        raise ValueError(
            "cold first Drum Pitch read did not use Natural Minor at root C"
        )

    # A panel event is the other legal first-use path.  Once initialized, an
    # explicit Shift + Scales OFF must remain off through a scale commit and a
    # later pitch read; otherwise the initialization marker is not doing its job.
    cold_panel = Emulator(patched)
    enter_panel_event(cold_panel, 0x00)
    if cold_panel.read_byte(STATE_SCALE_MODE) != MODE_ON_STATE:
        raise ValueError("cold first panel event did not initialize mode ON")
    if cold_panel.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F != 0x0F:
        raise ValueError("cold first panel event did not refresh all Drum pitches")

    cold_shift = Emulator(patched)
    enter_panel_event(cold_shift, SHIFT_LOGICAL_ID)
    if cold_shift.read_byte(STATE_SCALE_MODE) != (
        MODE_ON_STATE | SHIFT_ARMED_BIT
    ):
        raise ValueError("cold first Shift event was lost during default-on refresh")
    enter_panel_event(cold_shift, SCALES_LOGICAL_ID)
    cold_shift.run(
        TOGGLE_SCALES_CASE,
        stack_words=(0, 0, 0, 0, 0, RETURN_SENTINEL | 1),
    )
    if cold_shift.read_byte(STATE_SCALE_MODE) != MODE_OFF_STATE:
        raise ValueError("cold first Shift + Scales did not toggle mode OFF")

    enter_panel_event(cold_panel, SHIFT_LOGICAL_ID)
    enter_panel_event(cold_panel, SCALES_LOGICAL_ID)
    cold_panel.run(
        TOGGLE_SCALES_CASE,
        stack_words=(0, 0, 0, 0, 0, RETURN_SENTINEL | 1),
    )
    if cold_panel.read_byte(STATE_SCALE_MODE) != MODE_OFF_STATE:
        raise ValueError("first-session explicit OFF lost the initialized marker")
    child, _ = configure_held_scales_ui(cold_panel)
    cold_panel.run(
        STOCK_UI_SCALE_HANDLER,
        r0=child,
        r1=14,
        max_instructions=10000,
    )
    if cold_panel.read_byte(STATE_SCALE_MODE) != (MODE_OFF_STATE | 14):
        raise ValueError("mode-off scale commit changed the initialized/off flags")
    cold_panel.raw_pitch = 72
    if cold_panel.run(MAPPED_PITCH_GETTER, r0=0x10, r1=2) != 72:
        raise ValueError("later Drum Pitch read silently re-enabled explicit OFF")
    if cold_panel.read_byte(STATE_SCALE_MODE) != (MODE_OFF_STATE | 14):
        raise ValueError("later Drum Pitch read corrupted explicit OFF state")

    # Scale and root mirrors must alter only their own fields, request all four
    # stock Drum callbacks, and let the stock dirty-bit dispatcher apply them.
    base_state = MODE_ON_STATE | 1
    emulator.write_byte(STATE_SCALE_MODE, base_state)
    emulator.write_byte(STATE_ROOT, 3)
    emulator.raw_pitch = 72
    emulator.drum_getter_calls.clear()
    emulator.dsp_calls.clear()
    isolated_scale_output = 0x20002000
    emulator.write_word(
        isolated_scale_output + 4,
        emulator.read_word(STOCK_SCALE_MASKS + 14 * 4),
    )
    emulator.run(
        UPDATE_SCALE,
        r0=isolated_scale_output,
        r2=14,
        max_instructions=10000,
    )
    expected = MODE_ON_STATE | 14
    if emulator.read_byte(STATE_SCALE_MODE) != expected:
        raise ValueError("scale state mirror corrupted reserved state")
    if emulator.read_byte(STATE_ROOT) != 3:
        raise ValueError("scale state mirror corrupted root state")
    assert_refresh(
        emulator,
        72,
        14,
        3,
        enabled=True,
        label="master scale mirror",
    )

    # The entry wrapper captures root without modifying the unsafe root tail.
    emulator.write_byte(STATE_ROOT, 0)
    emulator.drum_getter_calls.clear()
    emulator.dsp_calls.clear()
    emulator.run(
        UPDATE_ROOT_ENTRY,
        r0=0x20006100,
        r4=master,
        max_instructions=10000,
    )
    if emulator.read_byte(STATE_ROOT) != root_note:
        raise ValueError("entry wrapper did not capture the raw root")
    if emulator.read_byte(STATE_SCALE_MODE) != expected:
        raise ValueError("root state mirror corrupted scale/mode state")
    assert_refresh(
        emulator,
        72,
        14,
        root_note,
        enabled=True,
        label="master root mirror",
    )

    # The real main-handler event sequence must arm Shift, preserve that arm
    # through Scales, clear it on any intervening panel event, and consume it
    # on the toggle.  Turning mode off restores raw stock pitch immediately;
    # turning it on applies the scale mapping.  Scales alone stays stock.
    stack_frame = (0, 0, 0, 0, 0, RETURN_SENTINEL | 1)
    emulator.write_byte(STATE_SCALE_MODE, expected)
    enter_panel_event(emulator, SHIFT_LOGICAL_ID)
    armed = expected | SHIFT_ARMED_BIT
    if emulator.read_byte(STATE_SCALE_MODE) != armed:
        raise ValueError("Shift event did not arm the event-sequence detector")
    enter_panel_event(emulator, SCALES_LOGICAL_ID)
    if emulator.read_byte(STATE_SCALE_MODE) != armed:
        raise ValueError("Scales event did not preserve the Shift arm")
    emulator.drum_getter_calls.clear()
    emulator.dsp_calls.clear()
    emulator.run(TOGGLE_SCALES_CASE, stack_words=stack_frame)
    toggled = emulator.read_byte(STATE_SCALE_MODE)
    if toggled != (expected ^ MODE_BIT):
        raise ValueError("Shift + Scales did not toggle exactly the mode bit")
    if emulator.view_select_calls:
        raise ValueError("Shift + Scales unexpectedly opened Scales View")
    assert_refresh(
        emulator,
        72,
        14,
        root_note,
        enabled=False,
        label="mode-off toggle",
    )

    emulator.drum_getter_calls.clear()
    emulator.dsp_calls.clear()
    enter_panel_event(emulator, SHIFT_LOGICAL_ID)
    enter_panel_event(emulator, SCALES_LOGICAL_ID)
    emulator.run(TOGGLE_SCALES_CASE, stack_words=stack_frame)
    if emulator.read_byte(STATE_SCALE_MODE) != expected:
        raise ValueError("second Shift + Scales did not re-enable the mode")
    assert_refresh(
        emulator,
        72,
        14,
        root_note,
        enabled=True,
        label="mode-on toggle",
    )

    # An intervening non-Scales panel event must cancel a stale Shift arm.
    enter_panel_event(emulator, SHIFT_LOGICAL_ID)
    enter_panel_event(emulator, 0x00)
    if emulator.read_byte(STATE_SCALE_MODE) != expected:
        raise ValueError("intervening panel event did not clear the Shift arm")

    emulator.view_select_calls.clear()
    emulator.drum_getter_calls.clear()
    emulator.dsp_calls.clear()
    main_object = 0x20003000
    emulator.run(
        TOGGLE_SCALES_CASE,
        r4=main_object,
        stack_words=(0, 0, 0, 0, 0, RETURN_SENTINEL | 1),
    )
    if emulator.view_select_calls != [(main_object, 4, 0x12)]:
        raise ValueError(
            f"normal Scales path changed: {emulator.view_select_calls}"
        )
    if (
        emulator.drum_getter_calls
        or emulator.dsp_calls
        or emulator.read_byte(STOCK_DRUM_DIRTY_FLAGS) & 0x0F
    ):
        raise ValueError("normal Scales path unexpectedly refreshed drum pitch")

    # Exercise the refresh request and real dirty-bit dispatcher across both
    # sides of the stock signed divide-by-two conversion, in both mode states.
    scheduled_refresh_cases = 0
    for enabled in (False, True):
        for value in (0, 37, 64, 91, 127):
            emulator.raw_pitch = value
            emulator.write_byte(
                STATE_SCALE_MODE,
                (MODE_ON_STATE if enabled else MODE_OFF_STATE) | 1,
            )
            emulator.write_byte(STATE_ROOT, 3)
            emulator.write_byte(STOCK_DRUM_DIRTY_FLAGS, 0xA0)
            emulator.drum_getter_calls.clear()
            emulator.dsp_calls.clear()
            emulator.run(REFRESH_ALL_DRUM_PITCH, max_instructions=10000)
            if emulator.drum_getter_calls or emulator.dsp_calls:
                raise ValueError(
                    "refresh request bypassed the stock dirty-bit dispatcher"
                )
            assert_refresh(
                emulator,
                value,
                1,
                3,
                enabled=enabled,
                label=f"scheduled refresh CC={value}, enabled={enabled}",
            )
            if emulator.read_byte(STOCK_DRUM_DIRTY_FLAGS) != 0xA0:
                raise ValueError("refresh request corrupted unrelated dirty bits")
            scheduled_refresh_cases += 1

    # Execute the injected ARM mapper against the Python specification.
    cases = 0
    reload_cases = 0
    for scale in range(16):
        for root in range(12):
            emulator.write_byte(
                STATE_SCALE_MODE,
                MODE_ON_STATE | (scale & SCALE_MASK),
            )
            emulator.write_byte(STATE_ROOT, root + 12)
            for value in range(128):
                emulator.raw_pitch = value
                actual = emulator.run(
                    MAPPED_PITCH_GETTER,
                    r0=0x10,
                    r1=2,
                    max_instructions=1000,
                )
                expected_pitch = map_pitch(value, scale, root)
                if actual != expected_pitch:
                    raise ValueError(
                        f"ARM mapper mismatch: scale={scale}, root={root}, "
                        f"CC={value}, expected={expected_pitch}, got={actual}"
                    )
                cases += 1

                emulator.raw_pitch = value
                reload_actual = emulator.run(
                    MAPPED_PATTERN_PITCH_GETTER,
                    r0=3,
                    r1=2,
                    max_instructions=1000,
                )
                if reload_actual != expected_pitch:
                    raise ValueError(
                        f"reload mapper mismatch: scale={scale}, root={root}, "
                        f"CC={value}, expected={expected_pitch}, got={reload_actual}"
                    )
                reload_cases += 1

    emulator.write_byte(STATE_SCALE_MODE, MODE_OFF_STATE | 15)
    emulator.write_byte(STATE_ROOT, 11)
    for value in range(128):
        emulator.raw_pitch = value
        actual = emulator.run(MAPPED_PITCH_GETTER, r0=0x10, r1=2)
        if actual != value:
            raise ValueError(
                f"mode-off regression at CC {value}: returned {actual}"
            )
        reload_actual = emulator.run(
            MAPPED_PATTERN_PITCH_GETTER,
            r0=3,
            r1=2,
        )
        if reload_actual != value:
            raise ValueError(
                f"mode-off reload regression at CC {value}: returned {reload_actual}"
            )

    # Exercise each real live Drum callback around the patched call site.  In
    # mode off every DSP write must remain bit-for-bit stock.  In mode on the
    # pitch write must contain the mapped value after the stock conversion.
    callback_cases = 0
    for callback, pitch_register in LIVE_DRUM_PITCH_CALLBACKS:
        for value in (0, 37, 64, 91, 127):
            stock_emulator.raw_pitch = value
            emulator.raw_pitch = value
            stock_emulator.dsp_calls.clear()
            emulator.dsp_calls.clear()
            emulator.write_byte(STATE_SCALE_MODE, MODE_OFF_STATE)
            stock_emulator.run(callback, max_instructions=10000)
            emulator.run(callback, max_instructions=10000)
            if emulator.dsp_calls != stock_emulator.dsp_calls:
                raise ValueError(
                    f"mode-off live callback regression at {callback:#010x}, "
                    f"CC={value}: stock={stock_emulator.dsp_calls}, "
                    f"patched={emulator.dsp_calls}"
                )

            scale = 1
            root = 3
            emulator.dsp_calls.clear()
            emulator.write_byte(STATE_SCALE_MODE, MODE_ON_STATE | scale)
            emulator.write_byte(STATE_ROOT, root)
            emulator.run(callback, max_instructions=10000)
            writes = [
                data for data, register in emulator.dsp_calls
                if register == pitch_register
            ]
            if len(writes) != 1:
                raise ValueError(
                    f"live callback {callback:#010x} made {len(writes)} "
                    f"writes to pitch register {pitch_register:#06x}"
                )
            mapped = map_pitch(value, scale, root)
            expected_dsp = stock_pitch_dsp_value(mapped)
            if writes[0] != expected_dsp:
                raise ValueError(
                    f"live callback {callback:#010x} pitch mismatch at CC={value}: "
                    f"expected {expected_dsp:#010x}, got {writes[0]:#010x}"
                )
            callback_cases += 1

    ui_results = held_scales_ui_checks(stock, patched)

    return {
        "arm_mapping_cases": cases,
        "reload_mapping_cases": reload_cases,
        "cold_boot_default_on": "passed through both pitch-read and panel-event paths",
        "explicit_off_persistence": "passed across scale commit and later pitch read",
        "mode_off_cases": 128,
        "reserved_state_bytes": "passed",
        "scale_root_state_mirroring": "passed",
        "authoritative_scale_root_capture_without_change_flags": "passed",
        "drum_retrigger_on_toggle_scale_root": "passed",
        "complete_master_update_path": "passed",
        "stock_r0_return_contract": "passed",
        "shift_scales_toggle": "passed",
        "shift_event_sequence": (
            "0x1B arms; Scales preserves/consumes; intervening panel event clears"
        ),
        "normal_scales_fallback": "passed",
        "scheduled_refresh_cases": scheduled_refresh_cases,
        "live_drum_callback_cases": callback_cases,
        "mode_off_live_dsp_writes": "bit-for-bit stock",
        **ui_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stock",
        type=Path,
        default=Path("build")
        / "circuit-scale-follow"
        / "circuit-3592-stock.bin",
    )
    parser.add_argument(
        "--patched",
        type=Path,
        default=Path("build")
        / "circuit-scale-follow"
        / "circuit-3592-scale-follow-distortion-select.bin",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("build")
        / "circuit-scale-follow"
        / "circuit-3592-scale-follow-distortion-select-manifest.json",
    )
    args = parser.parse_args()

    stock = args.stock.read_bytes()
    patched = args.patched.read_bytes()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = {
        "static": static_checks(stock, patched, manifest),
        "emulation": emulation_checks(stock, patched),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
