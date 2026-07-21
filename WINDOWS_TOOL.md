# Windows one-click tool

`CircuitScaleFollowTool.exe` builds and uploads Circuit Scale Follow without requiring Python. It contains the patcher and MIDI sender, but **no Novation firmware**.

## Before starting

- Use an original Novation Circuit running firmware 1.8 build 3592.
- Obtain your own legitimate `circuit-firmware-3592.syx` from Novation's official software/update materials.
- Back up your Circuit pack in Novation Components.
- Close Components, DAWs, MIDI utilities, and anything else that may hold the MIDI port.
- Connect reliable external power. Do not update on batteries alone.

## Download and verify

Download these two files from the latest GitHub release:

- `CircuitScaleFollowTool-v0.2.0-Windows-x64.zip`
- `CircuitScaleFollowTool-v0.2.0-Windows-x64.zip.sha256.txt`

In PowerShell, verify the ZIP before extracting it:

```powershell
Get-FileHash .\CircuitScaleFollowTool-v0.2.0-Windows-x64.zip -Algorithm SHA256
Get-Content .\CircuitScaleFollowTool-v0.2.0-Windows-x64.zip.sha256.txt
```

The two hashes must match exactly. The executable is not commercially code-signed, so Windows may identify the publisher as unknown. The source and reproducible build script are included in this repository.

## Build the firmware

1. Extract the ZIP and run `CircuitScaleFollowTool.exe`.
2. Choose your stock `circuit-firmware-3592.syx`.
3. Choose an output folder.
4. Click **Validate and Build Firmware**.

The tool accepts only this stock SysEx SHA-256:

```text
260a72ebd10208aae44f7c01ad18a79cf1d7ad32658ecd1dee0d5215c0e6b7c0
```

It creates:

- `circuit-3592-scale-follow.syx` — the locally generated mod
- `circuit-3592-stock-recovery.syx` — untouched recovery firmware
- `circuit-3592-scale-follow-manifest.json` — verification and patch metadata
- decoded `.bin` images for technical inspection

The expected mod SHA-256 is:

```text
af80b145fb5aa122eab4c7146b409c36a16bab19765b71369b9e9e6ee448d3ef
```

Upload remains disabled unless that exact result is produced.

## Upload the mod

1. Turn the original Circuit off.
2. Hold **Scales + Note + Velocity** while powering it on.
3. In the tool, click **Refresh** and select the MIDI output connected to the Circuit.
4. Click **Upload Verified Firmware**.
5. Read the final confirmation carefully and begin the transfer.
6. Do not touch power, USB, or MIDI cables for approximately 2¼ minutes.
7. Wait for the Circuit to finish or restart before disconnecting anything.

Scale Follow is on after boot. Hold **Shift** and press **Scales** to toggle it. For ordinary root/scale selection, enter the normal **Scales** view and choose the new setting; Scales does not need to remain held.

## Recovery

If a transfer fails, leave the Circuit powered. Reopen the tool and click **Upload Stock Recovery**, or reinstall stock firmware through Novation's official updater. Do not repeatedly power-cycle a unit left in an incomplete update state.

The GUI uploads only the generated Scale Follow file or its exact stock recovery file. Both hashes are rechecked immediately before transfer, preventing accidental selection of unrelated or damaged firmware.

## Reproduce the Windows package

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
.\scripts\build_windows_release.ps1
```

Generated release files are written to `release-build/artifacts/` and are excluded from Git.
