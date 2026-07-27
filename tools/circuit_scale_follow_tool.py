"""One-click builder/uploader for Scale Follow, Distortion Select, and Sample Start."""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import rtmidi

from build_circuit_scale_follow import (
    PATCHED_SYSEX_SHA256,
    RELEASE_VERSION,
    STOCK_SYSEX_SHA256,
    BuildArtifacts,
    build_from_sysex,
)
from circuit_scale_follow import self_test as mapping_self_test
from send_circuit_firmware import find_port, validate_firmware


APP_NAME = "Circuit Scale Follow Tool"
TRANSFER_INTERVAL_SECONDS = 0.022


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class BuiltRelease:
    artifacts: BuildArtifacts
    patched_sha256: str
    message_count: int


def build_release(stock_sysex: Path, output_dir: Path) -> BuiltRelease:
    artifacts = build_from_sysex(stock_sysex, output_dir)
    digest = sha256_file(artifacts.patched_sysex)
    if digest != PATCHED_SYSEX_SHA256:
        raise ValueError(
            "generated firmware hash does not match the hardware-tested release"
        )
    messages, _ = validate_firmware(artifacts.patched_sysex.read_bytes())
    return BuiltRelease(artifacts, digest, len(messages))


def send_release(
    firmware: Path,
    port_name: str,
    expected_sha256: str,
    firmware_label: str,
    progress: Callable[[int, int], None],
    log: Callable[[str], None],
) -> None:
    data = firmware.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f"selected firmware is not the verified {firmware_label}")

    messages, counts = validate_firmware(data)
    midi_out = rtmidi.MidiOut()
    ports = midi_out.get_ports()
    port_index, exact_port_name = find_port(ports, port_name)
    log(f"MIDI output: {exact_port_name}")
    log(f"Firmware SHA-256: {digest}")
    log(f"Messages: {counts}")

    midi_out.open_port(port_index, APP_NAME)
    log("TRANSFER STARTED — do not power off or disconnect the Circuit.")
    started = time.monotonic()
    try:
        for index, message in enumerate(messages, start=1):
            try:
                midi_out.send_message(list(message.raw))
            except Exception as exc:
                if index == 1:
                    raise RuntimeError(
                        "Windows rejected the first SysEx message before a firmware "
                        "block was accepted. Close Components, DAWs, browser MIDI "
                        "pages, and other MIDI tools; connect the Circuit directly; "
                        "enter bootloader mode; click Refresh; and select the Circuit "
                        "Bootloader output. Original error: "
                        f"{exc}"
                    ) from exc
                raise
            if message.command == 0x71:
                time.sleep(0.5)
            else:
                time.sleep(TRANSFER_INTERVAL_SECONDS)
            if index == 1 or index % 50 == 0 or index == len(messages):
                progress(index, len(messages))
    finally:
        midi_out.close_port()

    elapsed = time.monotonic() - started
    log(f"Transfer complete in {elapsed:.1f} seconds.")
    log("Wait for the Circuit to finish or restart before touching power or cables.")


class CircuitToolApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {RELEASE_VERSION}")
        self.root.geometry("790x690")
        self.root.minsize(720, 620)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.events: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self.stock_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose your legitimate stock 3592 SysEx.")
        self.built_release: BuiltRelease | None = None
        self.build_busy = False
        self.transfer_active = False

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Circuit Scale Follow",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "Build and install Scale Follow plus per-drum distortion selection "
                "for the original Circuit, firmware 1.8 build 3592."
            ),
            wraplength=740,
        ).pack(anchor="w", pady=(2, 14))

        build_box = ttk.LabelFrame(outer, text="1. Build from your stock firmware", padding=12)
        build_box.pack(fill="x")
        build_box.columnconfigure(1, weight=1)

        ttk.Label(build_box, text="Stock 3592 SysEx:").grid(row=0, column=0, sticky="w")
        ttk.Entry(build_box, textvariable=self.stock_var).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(build_box, text="Browse…", command=self._choose_stock).grid(
            row=0, column=2
        )

        ttk.Label(build_box, text="Output folder:").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Entry(build_box, textvariable=self.output_var).grid(
            row=1, column=1, sticky="ew", padx=8, pady=(8, 0)
        )
        ttk.Button(build_box, text="Browse…", command=self._choose_output).grid(
            row=1, column=2, pady=(8, 0)
        )

        self.build_button = ttk.Button(
            build_box,
            text="Validate and Build Firmware",
            command=self._start_build,
        )
        self.build_button.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))

        upload_box = ttk.LabelFrame(outer, text="2. Upload to the Circuit", padding=12)
        upload_box.pack(fill="x", pady=(14, 0))
        upload_box.columnconfigure(1, weight=1)

        ttk.Label(
            upload_box,
            text=(
                "Power off. Hold SCALES + NOTE + VELOCITY while powering on. "
                "Keep reliable power connected."
            ),
            wraplength=730,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(upload_box, text="MIDI output:").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        self.port_combo = ttk.Combobox(
            upload_box,
            textvariable=self.port_var,
            state="readonly",
        )
        self.port_combo.grid(row=1, column=1, sticky="ew", padx=8, pady=(10, 0))
        ttk.Button(upload_box, text="Refresh", command=self._refresh_ports).grid(
            row=1, column=2, pady=(10, 0)
        )

        self.upload_button = ttk.Button(
            upload_box,
            text="Upload Verified Firmware",
            command=lambda: self._confirm_upload(recovery=False),
            state="disabled",
        )
        self.upload_button.grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0)
        )
        self.recovery_button = ttk.Button(
            upload_box,
            text="Upload Stock Recovery",
            command=lambda: self._confirm_upload(recovery=True),
            state="disabled",
        )
        self.recovery_button.grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )

        status_box = ttk.LabelFrame(outer, text="Status", padding=12)
        status_box.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(status_box, textvariable=self.status_var, wraplength=720).pack(
            anchor="w"
        )
        self.progress = ttk.Progressbar(status_box, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 8))
        self.log_widget = tk.Text(
            status_box,
            height=11,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        self.log_widget.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text=(
                "No Novation firmware is included. The tool accepts only the exact "
                "verified stock file and always creates a stock recovery copy."
            ),
            wraplength=740,
        ).pack(anchor="w", pady=(10, 0))

        self._refresh_ports()
        self.root.after(100, self._poll_events)

    def run(self) -> None:
        self.root.mainloop()

    def _choose_stock(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askopenfilename(
            title="Choose stock Circuit 1.8 build 3592 firmware",
            filetypes=(("SysEx firmware", "*.syx"), ("All files", "*.*")),
        )
        if selected:
            path = Path(selected)
            self.stock_var.set(str(path))
            if not self.output_var.get():
                self.output_var.set(
                    str(path.parent / f"Circuit-Scale-Follow-{RELEASE_VERSION}")
                )

    def _choose_output(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(title="Choose output folder")
        if selected:
            self.output_var.set(selected)

    def _refresh_ports(self) -> None:
        midi_out = rtmidi.MidiOut()
        try:
            ports = midi_out.get_ports()
        finally:
            midi_out.close_port()
        self.port_combo["values"] = ports
        if ports:
            if self.port_var.get() not in ports:
                self.port_var.set(ports[0])
        else:
            self.port_var.set("")
        self._update_upload_state()

    def _start_build(self) -> None:
        from tkinter import messagebox

        stock = Path(self.stock_var.get().strip())
        output_text = self.output_var.get().strip()
        if not stock.is_file():
            messagebox.showerror(APP_NAME, "Choose an existing stock firmware SysEx file.")
            return
        if not output_text:
            messagebox.showerror(APP_NAME, "Choose an output folder.")
            return

        self.build_busy = True
        self.built_release = None
        self.build_button.configure(state="disabled")
        self.upload_button.configure(state="disabled")
        self.recovery_button.configure(state="disabled")
        self.progress["value"] = 0
        self.status_var.set("Validating stock firmware and building…")
        self._log(f"Input: {stock}")
        self._log(f"Expected stock SHA-256: {STOCK_SYSEX_SHA256}")

        def worker() -> None:
            try:
                release = build_release(stock, Path(output_text))
                self.events.put(("build_done", release))
            except Exception as exc:
                self.events.put(("error", "Build failed", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_upload(self, recovery: bool) -> None:
        from tkinter import messagebox

        if self.built_release is None:
            return
        port = self.port_var.get()
        if not port:
            messagebox.showerror(APP_NAME, "Select a MIDI output.")
            return
        if recovery:
            firmware = self.built_release.artifacts.stock_recovery_sysex
            expected_hash = STOCK_SYSEX_SHA256
            firmware_label = "stock recovery firmware"
        else:
            firmware = self.built_release.artifacts.patched_sysex
            expected_hash = PATCHED_SYSEX_SHA256
            firmware_label = "Scale Follow + Distortion Select + Sample Start firmware"
        warning = (
            f"This will send the verified {firmware_label}:\n\n{firmware}\n\n"
            f"to MIDI output:\n\n{port}\n\n"
            "Confirm the ORIGINAL Circuit is in bootloader mode, power is stable, "
            "and you will not disconnect anything for about 2¼ minutes.\n\n"
            "Begin the firmware transfer?"
        )
        if not messagebox.askyesno(APP_NAME, warning, icon="warning"):
            return
        self._start_upload(firmware, port, expected_hash, firmware_label)

    def _start_upload(
        self,
        firmware: Path,
        port: str,
        expected_hash: str,
        firmware_label: str,
    ) -> None:
        self.transfer_active = True
        self.build_button.configure(state="disabled")
        self.upload_button.configure(state="disabled")
        self.recovery_button.configure(state="disabled")
        self.progress["value"] = 0
        self.status_var.set("Uploading — do not power off or disconnect the Circuit.")

        def progress(current: int, total: int) -> None:
            self.events.put(("progress", current, total))

        def log(message: str) -> None:
            self.events.put(("log", message))

        def worker() -> None:
            try:
                send_release(
                    firmware,
                    port,
                    expected_hash,
                    firmware_label,
                    progress,
                    log,
                )
                self.events.put(("send_done", firmware_label))
            except Exception as exc:
                self.events.put(("error", "Transfer failed", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        from tkinter import messagebox

        while not self.events.empty():
            event = self.events.get()
            kind = event[0]
            if kind == "log":
                self._log(event[1])
            elif kind == "progress":
                current, total = event[1], event[2]
                self.progress["value"] = current * 100 / total
                self.status_var.set(f"Uploading message {current:,} of {total:,}…")
            elif kind == "build_done":
                self.build_busy = False
                self.built_release = event[1]
                release = self.built_release
                self.progress["value"] = 100
                self.status_var.set("Build verified. Put the Circuit in bootloader mode.")
                self._log(f"Patched firmware: {release.artifacts.patched_sysex}")
                self._log(
                    f"Stock recovery: {release.artifacts.stock_recovery_sysex}"
                )
                self._log(f"Patched SHA-256: {release.patched_sha256}")
                self._log(f"Validated messages: {release.message_count}")
                self.build_button.configure(state="normal")
                self._update_upload_state()
                messagebox.showinfo(
                    APP_NAME,
                    "Firmware built and verified. A stock recovery copy was also saved.",
                )
            elif kind == "send_done":
                self.transfer_active = False
                self.progress["value"] = 100
                self.status_var.set(
                    f"{event[1].capitalize()} transfer complete. Cold-power-cycle before testing."
                )
                self.build_button.configure(state="normal")
                self._update_upload_state()
                messagebox.showinfo(
                    APP_NAME,
                    "Transfer complete. First let the Circuit finish or restart. Then turn it off, "
                    "remove USB, MIDI, external power, and batteries (if fitted) for 10 seconds, "
                    "and start normally. This one-time cold boot clears retained bootloader RAM "
                    "so Scale Follow initializes on.",
                )
            elif kind == "error":
                was_transfer = self.transfer_active
                self.build_busy = False
                self.transfer_active = False
                self.build_button.configure(state="normal")
                self._update_upload_state()
                self.status_var.set(event[1])
                self._log(f"{event[1]}: {event[2]}")
                extra = (
                    "\n\nLeave the Circuit powered and use the stock recovery file."
                    if was_transfer
                    else ""
                )
                messagebox.showerror(APP_NAME, event[1] + ":\n\n" + event[2] + extra)
        self.root.after(100, self._poll_events)

    def _update_upload_state(self) -> None:
        enabled = (
            self.built_release is not None
            and bool(self.port_var.get())
            and not self.build_busy
            and not self.transfer_active
        )
        self.upload_button.configure(state="normal" if enabled else "disabled")
        self.recovery_button.configure(state="normal" if enabled else "disabled")

    def _log(self, message: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", message + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _close(self) -> None:
        from tkinter import messagebox

        if self.build_busy:
            messagebox.showwarning(
                APP_NAME,
                "Firmware validation and build are still running. Wait for them to finish before closing.",
            )
            return
        if self.transfer_active:
            messagebox.showwarning(
                APP_NAME,
                "The firmware transfer is active. Do not close this tool or disconnect the Circuit.",
            )
            return
        self.root.destroy()


def headless_build(stock: Path, output_dir: Path) -> int:
    release = build_release(stock, output_dir)
    report = {
        "release": RELEASE_VERSION,
        "patched_sysex": str(release.artifacts.patched_sysex),
        "patched_sysex_sha256": release.patched_sha256,
        "stock_recovery_sysex": str(release.artifacts.stock_recovery_sysex),
        "message_count": release.message_count,
        "verification": release.artifacts.verification,
    }
    report_path = output_dir / "circuit-scale-follow-tool-report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=RELEASE_VERSION)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--headless-build", type=Path, metavar="STOCK_SYX")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    if args.self_test:
        mapping_self_test()
        print("Scale Follow mapping self-test passed.")
        return 0
    if args.headless_build:
        output_dir = args.output_dir or (
            args.headless_build.parent / f"Circuit-Scale-Follow-{RELEASE_VERSION}"
        )
        return headless_build(args.headless_build, output_dir)
    if args.output_dir:
        parser.error("--output-dir requires --headless-build")

    CircuitToolApp().run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if getattr(sys, "frozen", False):
            try:
                from tkinter import messagebox

                messagebox.showerror(APP_NAME, str(error))
            except Exception:
                pass
        raise
