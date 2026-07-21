"""Decode, patch, and re-encode the original Novation Circuit firmware SysEx.

The original Circuit update stream contains one UPDATE_INIT message, many
UPDATE_WRITE messages, and one UPDATE_FINISH message.  UPDATE_FINISH carries
the *first* 32 bytes of the firmware image; the UPDATE_WRITE payloads carry
the remainder in order.

Each 32-byte firmware block is represented as 37 MIDI-safe 7-bit bytes:
the 256 firmware bits are split into 7-bit groups and padded with three zero
bits at the end.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


NOVATION_HEADER = bytes((0xF0, 0x00, 0x20, 0x29, 0x00))
UPDATE_INIT = 0x71
UPDATE_WRITE = 0x72
UPDATE_FINISH = 0x73
BLOCK_SIZE = 32
PACKED_BLOCK_SIZE = 37


@dataclass(frozen=True)
class SysexMessage:
    command: int
    raw: bytes

    @property
    def payload(self) -> bytes:
        return self.raw[6:-1]


def split_messages(data: bytes) -> list[SysexMessage]:
    messages: list[SysexMessage] = []
    cursor = 0
    while cursor < len(data):
        start = data.find(b"\xF0", cursor)
        if start < 0:
            break
        end = data.find(b"\xF7", start + 1)
        if end < 0:
            raise ValueError(f"unterminated SysEx message at offset {start}")
        raw = data[start : end + 1]
        if not raw.startswith(NOVATION_HEADER):
            raise ValueError(f"unexpected SysEx manufacturer header at offset {start}")
        if len(raw) < 7:
            raise ValueError(f"short SysEx message at offset {start}")
        messages.append(SysexMessage(raw[5], raw))
        cursor = end + 1
    return messages


def decode_block(payload: bytes) -> bytes:
    if len(payload) != PACKED_BLOCK_SIZE:
        raise ValueError(f"expected {PACKED_BLOCK_SIZE} packed bytes, got {len(payload)}")
    if any(value & 0x80 for value in payload):
        raise ValueError("packed firmware payload contains a non-MIDI-safe byte")
    bits = "".join(f"{value:07b}" for value in payload)
    return bytes(int(bits[index : index + 8], 2) for index in range(0, 256, 8))


def encode_block(block: bytes, padding: int = 0) -> bytes:
    if len(block) != BLOCK_SIZE:
        raise ValueError(f"expected a {BLOCK_SIZE}-byte firmware block")
    if not 0 <= padding <= 7:
        raise ValueError("padding must fit in three bits")
    bits = "".join(f"{value:08b}" for value in block) + f"{padding:03b}"
    return bytes(int(bits[index : index + 7], 2) for index in range(0, 259, 7))


def decode_firmware(data: bytes) -> tuple[bytes, list[SysexMessage]]:
    messages = split_messages(data)
    finish = [message for message in messages if message.command == UPDATE_FINISH]
    writes = [message for message in messages if message.command == UPDATE_WRITE]
    if len(finish) != 1:
        raise ValueError(f"expected one UPDATE_FINISH message, got {len(finish)}")
    if not writes:
        raise ValueError("firmware has no UPDATE_WRITE messages")
    image = decode_block(finish[0].payload)
    image += b"".join(decode_block(message.payload) for message in writes)
    return image, messages


def _replace_payload(message: SysexMessage, payload: bytes) -> bytes:
    if len(payload) != len(message.payload):
        raise ValueError("replacement payload length differs from original message")
    return message.raw[:6] + payload + message.raw[-1:]


def encode_firmware(image: bytes, messages: Iterable[SysexMessage]) -> bytes:
    messages = list(messages)
    if len(image) % BLOCK_SIZE:
        raise ValueError("firmware image length is not a multiple of 32 bytes")

    blocks = [
        image[index : index + BLOCK_SIZE]
        for index in range(0, len(image), BLOCK_SIZE)
    ]
    finish_indexes = [
        index for index, message in enumerate(messages)
        if message.command == UPDATE_FINISH
    ]
    write_indexes = [
        index for index, message in enumerate(messages)
        if message.command == UPDATE_WRITE
    ]
    if len(finish_indexes) != 1:
        raise ValueError("template does not contain exactly one UPDATE_FINISH")
    if len(blocks) != 1 + len(write_indexes):
        raise ValueError(
            f"image has {len(blocks)} blocks but template carries "
            f"{1 + len(write_indexes)}"
        )

    replacements: dict[int, bytes] = {
        finish_indexes[0]: encode_block(
            blocks[0],
            messages[finish_indexes[0]].payload[-1] & 0x07,
        ),
    }
    for index, block in zip(write_indexes, blocks[1:]):
        replacements[index] = encode_block(
            block,
            messages[index].payload[-1] & 0x07,
        )

    output = bytearray()
    for index, message in enumerate(messages):
        payload = replacements.get(index)
        output.extend(
            message.raw if payload is None else _replace_payload(message, payload)
        )
    return bytes(output)


def load(path: str | Path) -> tuple[bytes, list[SysexMessage], bytes]:
    sysex = Path(path).read_bytes()
    image, messages = decode_firmware(sysex)
    return image, messages, sysex


def roundtrip(path: str | Path) -> bool:
    image, messages, original = load(path)
    return encode_firmware(image, messages) == original


if __name__ == "__main__":
    import argparse
    import hashlib

    parser = argparse.ArgumentParser()
    parser.add_argument("sysex", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    firmware, template, original = load(args.sysex)
    rebuilt = encode_firmware(firmware, template)
    print(f"firmware bytes: {len(firmware)}")
    print(f"firmware sha256: {hashlib.sha256(firmware).hexdigest()}")
    print(f"byte-identical round trip: {rebuilt == original}")
    if args.output:
        args.output.write_bytes(firmware)
