"""Conservative SysEx sender for the original Novation Circuit bootloader.

The sender is dry-run by default.  A transfer requires both ``--send`` and an
exact copy of the file's SHA-256 in ``--confirm-hash``.  This is intentional:
selecting the wrong MIDI output during a firmware transfer is avoidable.
"""

from __future__ import annotations

import argparse
import hashlib
import queue
import sys
import time
from pathlib import Path

import rtmidi

from circuit_fw_tools import UPDATE_FINISH, UPDATE_INIT, UPDATE_WRITE, split_messages


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_port(ports: list[str], requested: str) -> tuple[int, str]:
    exact = [(index, name) for index, name in enumerate(ports) if name == requested]
    if len(exact) == 1:
        return exact[0]
    partial = [
        (index, name)
        for index, name in enumerate(ports)
        if requested.casefold() in name.casefold()
    ]
    if len(partial) != 1:
        raise ValueError(
            f"port selector {requested!r} matched {len(partial)} outputs: "
            f"{[name for _, name in partial]}"
        )
    return partial[0]


def validate_firmware(data: bytes) -> tuple[list, dict[str, int]]:
    messages = split_messages(data)
    counts = {
        "init": sum(message.command == UPDATE_INIT for message in messages),
        "write": sum(message.command == UPDATE_WRITE for message in messages),
        "finish": sum(message.command == UPDATE_FINISH for message in messages),
    }
    if counts != {"init": 1, "write": 5979, "finish": 1}:
        raise ValueError(f"unexpected Circuit update message counts: {counts}")
    if any(any(byte & 0x80 for byte in message.payload) for message in messages):
        raise ValueError("firmware contains a non-MIDI-safe payload byte")
    return messages, counts


def drain_input(midi_in: rtmidi.MidiIn, received: queue.SimpleQueue) -> None:
    while True:
        message = midi_in.get_message()
        if message is None:
            break
        received.put(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("firmware", type=Path, nargs="?")
    parser.add_argument("--list", action="store_true", help="list MIDI ports and exit")
    parser.add_argument("--port", help="unique MIDI output name or substring")
    parser.add_argument("--send", action="store_true", help="perform the transfer")
    parser.add_argument(
        "--confirm-hash",
        help="must exactly equal the selected firmware file's SHA-256",
    )
    parser.add_argument(
        "--interval-ms",
        type=float,
        default=22.0,
        help="delay after each 44-byte block (default: 22 ms, DIN-safe)",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="do not open the MIDI input (useful for unstable Windows bootloader drivers)",
    )
    parser.add_argument(
        "--start-message",
        type=int,
        default=0,
        help="first SysEx message to send (inclusive; default: 0)",
    )
    parser.add_argument(
        "--end-message",
        type=int,
        help="last SysEx message boundary (exclusive; default: all messages)",
    )
    args = parser.parse_args()

    midi_out = rtmidi.MidiOut()
    midi_in = rtmidi.MidiIn()
    outputs = midi_out.get_ports()
    inputs = midi_in.get_ports()

    if args.list:
        print("MIDI inputs:")
        for index, name in enumerate(inputs):
            print(f"  {index}: {name}")
        print("MIDI outputs:")
        for index, name in enumerate(outputs):
            print(f"  {index}: {name}")
        return
    if args.firmware is None:
        parser.error("firmware is required unless --list is used")

    data = args.firmware.read_bytes()
    messages, counts = validate_firmware(data)
    end_message = len(messages) if args.end_message is None else args.end_message
    if not 0 <= args.start_message < end_message <= len(messages):
        raise ValueError(
            f"invalid message range {args.start_message}:{end_message}; "
            f"firmware has {len(messages)} messages"
        )
    selected_messages = messages[args.start_message:end_message]
    digest = sha256(data)
    print(f"file: {args.firmware}")
    print(f"bytes: {len(data)}")
    print(f"sha256: {digest}")
    print(f"messages: {counts}")
    print(f"selected range: {args.start_message}:{end_message}")
    print(
        f"estimated transfer: "
        f"{len(selected_messages) * args.interval_ms / 1000:.1f} seconds"
    )

    if not args.send:
        print("dry run only; no MIDI data sent")
        return
    if args.confirm_hash != digest:
        raise ValueError("--confirm-hash does not exactly match the selected file")
    if not args.port:
        parser.error("--port is required with --send")
    if args.interval_ms < 15 and any("rubix" in name.casefold() for name in outputs):
        raise ValueError("interval below 15 ms is unsafe for a DIN transfer through Rubix")

    output_index, output_name = find_port(outputs, args.port)
    matching_inputs = [] if args.no_monitor else [
        (index, name)
        for index, name in enumerate(inputs)
        if args.port.casefold() in name.casefold()
        or output_name.casefold() in name.casefold()
    ]
    if len(matching_inputs) > 1:
        raise ValueError(f"input selector is ambiguous: {matching_inputs}")

    received: queue.SimpleQueue = queue.SimpleQueue()
    if matching_inputs:
        input_index, input_name = matching_inputs[0]
        midi_in.ignore_types(sysex=False, timing=True, active_sense=True)
        midi_in.open_port(input_index, "Circuit firmware response monitor")
        print(f"response input: {input_name}")
        drain_input(midi_in, received)
    else:
        input_name = None
        print("response input: none")

    midi_out.open_port(output_index, "Circuit firmware sender")
    print(f"firmware output: {output_name}")
    print("TRANSFER STARTED - do not power off or disconnect the Circuit")

    start = time.monotonic()
    response_count = 0
    try:
        for relative_index, message in enumerate(selected_messages):
            index = args.start_message + relative_index
            midi_out.send_message(list(message.raw))
            if message.command == UPDATE_INIT:
                time.sleep(0.5)
            else:
                time.sleep(args.interval_ms / 1000)

            if input_name:
                drain_input(midi_in, received)
                while not received.empty():
                    raw, delta = received.get()
                    response_count += 1
                    if raw and raw[0] == 0xF0:
                        print(f"response {response_count}: {bytes(raw).hex(' ')}")

            if relative_index and relative_index % 250 == 0:
                elapsed = time.monotonic() - start
                print(f"{index:4d}/{len(messages) - 1} messages, {elapsed:5.1f} s")
    except KeyboardInterrupt:
        print("TRANSFER INTERRUPTED - leave the unit powered and use stock recovery")
        raise
    finally:
        midi_out.close_port()
        if input_name:
            midi_in.close_port()

    elapsed = time.monotonic() - start
    print(
        f"RANGE {args.start_message}:{end_message} COMPLETE "
        f"in {elapsed:.1f} seconds; responses: {response_count}"
    )
    if end_message == len(messages):
        print("Wait for the Circuit to finish/restart before touching power or cables.")
    else:
        print(f"Keep the Circuit powered; next message is {end_message}.")


if __name__ == "__main__":
    main()
