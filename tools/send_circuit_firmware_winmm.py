"""Alternate Windows firmware uploader for the Novation Circuit.

Why this exists
---------------
The regular uploader and the GUI tool both drive MIDI through python-rtmidi.
When rtmidi's Windows backend refuses the very first SysEx it reports only

    MidiOutWinMM::sendMessage: error sending sysex message

which hides the actual Windows error code.  This tool talks to ``winmm.dll``
directly through ctypes, so it is a genuinely different code path and it prints
the real ``MMSYSERR`` value.  Failing on message 1 of 5981 almost always means
the port is held by another application (Novation Components, a DAW, a browser
tab using WebMIDI), which surfaces here as MMSYSERR_ALLOCATED.

It needs nothing but a stock Python install: no rtmidi, no pip, no build step.

Usage
-----
    python send_circuit_firmware_winmm.py --list
    python send_circuit_firmware_winmm.py firmware.syx --port "Bootloader" --send

Add ``--confirm-hash <sha256>`` to refuse anything but the file you intended.
Without ``--send`` it performs every step except the transfer.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import sys
import time
from ctypes import wintypes
from pathlib import Path

if sys.platform != "win32":
    raise SystemExit("this uploader is Windows-only; use send_circuit_firmware.py elsewhere")

winmm = ctypes.WinDLL("winmm")

MAXPNAMELEN = 32
MHDR_DONE = 0x00000001
CALLBACK_NULL = 0x00000000

MMSYSERR = {
    0: "MMSYSERR_NOERROR",
    1: "MMSYSERR_ERROR (unspecified driver error)",
    2: "MMSYSERR_BADDEVICEID (no such device)",
    3: "MMSYSERR_NOTENABLED",
    4: "MMSYSERR_ALLOCATED (the port is already in use by another program)",
    5: "MMSYSERR_INVALHANDLE",
    6: "MMSYSERR_NODRIVER (no driver present)",
    7: "MMSYSERR_NOMEM (cannot allocate or lock memory)",
    8: "MMSYSERR_NOTSUPPORTED",
    10: "MMSYSERR_INVALFLAG",
    11: "MMSYSERR_INVALPARAM",
    12: "MMSYSERR_HANDLEBUSY",
    64: "MIDIERR_UNPREPARED",
    65: "MIDIERR_STILLPLAYING",
    66: "MIDIERR_NOMAP",
    67: "MIDIERR_NOTREADY (the device is not ready to accept data)",
    68: "MIDIERR_NODEVICE",
}

HINT = {
    4: (
        "Another program is holding the MIDI port.  Fully quit Novation\n"
        "  Components (including any browser tab and any background or tray\n"
        "  process), close every DAW and MIDI utility, then unplug and replug\n"
        "  the Circuit and re-enter bootloader mode before retrying."
    ),
    6: "Windows has no driver bound to this port.  Replug the Circuit.",
    67: "The device is not ready.  Confirm the Circuit is in bootloader mode.",
}


class MIDIOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", ctypes.c_char * MAXPNAMELEN),
        ("wTechnology", wintypes.WORD),
        ("wVoices", wintypes.WORD),
        ("wNotes", wintypes.WORD),
        ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


class MIDIHDR(ctypes.Structure):
    pass


MIDIHDR._fields_ = [
    ("lpData", ctypes.c_char_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_void_p),
    ("dwFlags", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(MIDIHDR)),
    ("reserved", ctypes.c_void_p),
    ("dwOffset", wintypes.DWORD),
    ("dwReserved", ctypes.c_void_p * 8),
]


def describe(code: int) -> str:
    name = MMSYSERR.get(code, f"unknown error {code}")
    buffer = ctypes.create_string_buffer(256)
    if winmm.midiOutGetErrorTextA(code, buffer, 256) == 0:
        text = buffer.value.decode("latin-1", "replace").strip()
        if text:
            return f"{name}: {text}"
    return name


def check(code: int, what: str) -> None:
    if code == 0:
        return
    message = f"{what} failed -- {describe(code)}"
    hint = HINT.get(code)
    if hint:
        message += f"\n\n  {hint}"
    raise SystemExit(message)


def list_ports() -> list[str]:
    names = []
    for index in range(winmm.midiOutGetNumDevs()):
        caps = MIDIOUTCAPS()
        if winmm.midiOutGetDevCapsA(index, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            names.append(caps.szPname.decode("latin-1", "replace"))
        else:
            names.append("<unreadable>")
    return names


def split_sysex(data: bytes) -> list[bytes]:
    """Split a .syx file into complete F0..F7 messages."""
    messages = []
    start = None
    for index, value in enumerate(data):
        if value == 0xF0:
            start = index
        elif value == 0xF7 and start is not None:
            messages.append(data[start : index + 1])
            start = None
    return messages


def send_one(handle, payload: bytes, attempts: int) -> None:
    last = None
    for attempt in range(attempts):
        header = MIDIHDR()
        buffer = ctypes.create_string_buffer(payload, len(payload))
        header.lpData = ctypes.cast(buffer, ctypes.c_char_p)
        header.dwBufferLength = len(payload)
        header.dwBytesRecorded = len(payload)

        code = winmm.midiOutPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
        if code != 0:
            last = ("midiOutPrepareHeader", code)
            time.sleep(0.05)
            continue

        code = winmm.midiOutLongMsg(handle, ctypes.byref(header), ctypes.sizeof(header))
        if code != 0:
            winmm.midiOutUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
            last = ("midiOutLongMsg", code)
            time.sleep(0.05)
            continue

        deadline = time.monotonic() + 5.0
        while not (header.dwFlags & MHDR_DONE) and time.monotonic() < deadline:
            time.sleep(0.001)
        winmm.midiOutUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
        return
    check(last[1], last[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("firmware", type=Path, nargs="?")
    parser.add_argument("--list", action="store_true", help="list MIDI output ports and exit")
    parser.add_argument("--port", help="substring of the output port name")
    parser.add_argument("--send", action="store_true", help="actually transfer")
    parser.add_argument("--confirm-hash", help="required SHA-256 of the SysEx file")
    parser.add_argument("--interval-ms", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--settle-ms", type=float, default=250.0)
    args = parser.parse_args()

    ports = list_ports()
    if args.list or not args.firmware:
        if not ports:
            print("no MIDI output ports found")
        for index, name in enumerate(ports):
            print(f"  [{index}] {name}")
        if not args.firmware and not args.list:
            parser.error("a firmware .syx path is required")
        return

    data = args.firmware.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    print(f"file    {args.firmware}")
    print(f"sha256  {digest}")
    if args.confirm_hash and args.confirm_hash.lower() != digest:
        raise SystemExit("--confirm-hash does not match this file; refusing to send")

    messages = split_sysex(data)
    if not messages:
        raise SystemExit("no complete SysEx messages found in this file")
    print(f"messages {len(messages)}")

    if not ports:
        raise SystemExit("no MIDI output ports found")
    if args.port:
        matches = [i for i, n in enumerate(ports) if args.port.lower() in n.lower()]
        if not matches:
            print("available ports:")
            for index, name in enumerate(ports):
                print(f"  [{index}] {name}")
            raise SystemExit(f"no output port matching {args.port!r}")
        target = matches[0]
    else:
        for index, name in enumerate(ports):
            print(f"  [{index}] {name}")
        raise SystemExit("choose one with --port")
    print(f"port    [{target}] {ports[target]}")

    if not args.send:
        print("\ndry run only; add --send to transfer")
        return

    handle = wintypes.HANDLE()
    check(
        winmm.midiOutOpen(ctypes.byref(handle), target, 0, 0, CALLBACK_NULL),
        "midiOutOpen",
    )
    # Some drivers reject the first long message if it arrives immediately
    # after the port opens, so let the device settle before message 1.
    time.sleep(args.settle_ms / 1000.0)

    interval = args.interval_ms / 1000.0
    try:
        for index, payload in enumerate(messages, start=1):
            send_one(handle, payload, args.retries)
            if index == 1 or index % 250 == 0 or index == len(messages):
                print(f"  sent {index} of {len(messages)}")
            time.sleep(interval)
    finally:
        winmm.midiOutReset(handle)
        winmm.midiOutClose(handle)

    print("\ntransfer complete; let the Circuit finish restarting")


if __name__ == "__main__":
    main()
