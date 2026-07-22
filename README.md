# Circuit Scale Follow + Drum Distortion Select + Sample Start

Scale-quantized drum pitching, selectable distortion algorithms, and per-drum
sample-start control for the **original Novation Circuit**.

Circuit Scale Follow changes each Drum Pitch control into a four-octave musical range. The selected master root and scale determine the available notes, so drum samples can be played melodically without hunting for semitones. Version 0.3.0 exposed all seven distortion algorithms already present in the Circuit DSP. Version 0.4.0 adds independent sample-start control for all four drums.

## Compatibility

- Original Novation Circuit only
- Firmware **1.8, build 3592** only
- Not compatible with Circuit Tracks, Circuit Rhythm, or Circuit Mono Station
- Hardware-tested on one original Circuit; additional test reports are welcome

The patcher refuses any input whose SHA-256 does not exactly match the verified stock 3592 update. This is deliberate: do not bypass the check.

## Easiest installation on Windows

Download the latest `CircuitScaleFollowTool` package from [GitHub Releases](https://github.com/tracelistener/tracelistener-circuit-scale-follow/releases/latest). It is a self-contained graphical builder and guarded MIDI uploader; Python is not required.

The tool does **not** contain Novation firmware. Choose your legitimate stock `circuit-firmware-3592.syx`, click **Validate and Build Firmware**, put the original Circuit in bootloader mode, select the correct MIDI output, and click **Upload Verified Firmware**. A stock recovery copy is always created before upload is enabled.

After a successful upload, allow the Circuit to finish restarting, then perform one complete cold power cycle: turn it off and remove USB, MIDI, external power, and batteries (if fitted) for 10 seconds before starting normally. The bootloader uses a warm restart that can retain the previous Scale Follow RAM state; the cold boot guarantees default-on initialization.

See [WINDOWS_TOOL.md](WINDOWS_TOOL.md) for the complete walkthrough and checksum instructions. Novation Components cannot import a custom firmware file; its local SysEx importer is for patches and banks. Use the included guarded uploader for this mod.

## Scale Follow

- Scale Follow is on by default after every boot.
- Hold **Shift** and press **Scales** to toggle it on or off.
- To choose the master root or scale, enter the normal **Scales** view and select it; the Scales button does not need to remain held.
- Drum Pitch covers two scale octaves below to two scale octaves above the tonic.
- The center value, MIDI CC 73 value 64, treats the loaded sample's original pitch as **C**.
- Changing the master root or scale immediately changes the pitch mapping.
- All four drum tracks work in live control, automation, session load, and pattern reload paths.
- When Scale Follow is off, Drum Pitch is bit-for-bit stock behavior.

## Drum Distortion Select

- On the active drum pair, hold **Shift** and turn the normal distortion Macro:
  **Macro 5** selects the first drum and **Macro 6** selects the second.
- Clockwise movement selects the next algorithm; counter-clockwise movement selects the previous one.
- The seven algorithms are diode, valve, clipper, crossover, rectifier, bit reducer, and rate reducer.
- Each of the four drums keeps an independent algorithm selection for the current power session.
- Algorithm choices are runtime controls; they are not written into patterns, sessions, or packs and reset after reboot.
- Turning Macro 5/6 without Shift retains the normal continuous distortion-amount behavior.
- Selecting an algorithm does not move the stored distortion amount.
- A small pop while changing algorithms on sounding audio is expected because the DSP switches waveshaping functions immediately.

## Drum Sample Start

- On the active drum pair, hold **Shift** and turn the normal decay Macro:
  **Macro 3** controls the first drum and **Macro 4** controls the second.
- Drum 1/3 therefore use Shift + Macro 3; Drum 2/4 use Shift + Macro 4.
- Each encoder event moves two of the internal 0–127 offset units, providing approximately 64 useful positions across the selected sample.
- Counter-clockwise moves toward the sample beginning; clockwise moves toward the end.
- Each drum keeps an independent offset during the current power session.
- Offsets reset after reboot and are not written into patterns, sessions, or packs.
- Turning Macro 3/4 without Shift retains the normal decay behavior.
- Changing samples recomputes the saved proportional offset for the new sample length.
- The offset is applied only to new hits; an already sounding voice is not moved.
- Starting at an arbitrary waveform position can naturally produce a click on some source samples.

The modification changes firmware behavior only. It does not contain or replace samples, sessions, packs, or factory patterns.

## Build it

Requirements: Python 3.10 or newer and a legitimate copy of the original Circuit 1.8 build 3592 firmware update SysEx.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python tools\build_circuit_scale_follow.py circuit-firmware-3592.syx
python tools\verify_circuit_scale_follow.py
```

macOS/Linux activation and commands:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python tools/build_circuit_scale_follow.py circuit-firmware-3592.syx
python tools/verify_circuit_scale_follow.py
```

The accepted stock SysEx SHA-256 is:

```text
260a72ebd10208aae44f7c01ad18a79cf1d7ad32658ecd1dee0d5215c0e6b7c0
```

Successful builds are written under `build/circuit-scale-follow/`. The important files are:

- `circuit-3592-scale-follow-distortion-sample-start.syx` — combined firmware update
- `circuit-3592-stock-recovery.syx` — untouched recovery copy of your input
- `circuit-3592-scale-follow-distortion-sample-start-manifest.json` — exact patch sites and checksums

The deterministic combined version 0.4.0 SysEx SHA-256 is:

```text
0dbe6176965d0a96b74c6a41ad0c76aa8e8d52a09a875422e86d86e338fd4e95
```

## Verify it

The builder first checks the exact stock image, ARM and DSP code-cave signatures, every changed-byte boundary, MIDI-safe payload encoding, and all 24,576 combinations of 16 scales × 12 roots × 128 input values.

The separate verifier emulates the inserted Thumb code and its integration hooks. It covers cold boot, toggle state, all four live callbacks, all four reload paths, root and scale commits, refresh scheduling, and stock behavior while disabled.

## Install it

Custom firmware always carries risk. Back up your Circuit pack with Novation Components first, use reliable power, and never disconnect power or MIDI during transfer.

The included sender is dry-run by default and requires the exact file hash before it will transmit anything:

```powershell
python tools\send_circuit_firmware.py --list
python tools\send_circuit_firmware.py build\circuit-scale-follow\circuit-3592-scale-follow-distortion-sample-start.syx --port "YOUR MIDI PORT" --send --confirm-hash 0dbe6176965d0a96b74c6a41ad0c76aa8e8d52a09a875422e86d86e338fd4e95 --interval-ms 22 --no-monitor
```

To enter the original Circuit bootloader, turn the unit off, then hold **Scales + Note + Velocity** while powering on. Novation documents the same recovery procedure in its [official firmware-update support article](https://support.novationmusic.com/hc/en-gb/articles/360002211360-Updating-firmware-using-Novation-Components).

If anything goes wrong, keep the Circuit powered and send the generated `circuit-3592-stock-recovery.syx`, or reinstall stock firmware through Novation's official updater. Do not experiment with partial message ranges unless you are actively developing and understand the recovery process.

## Technical notes

The patch keeps the stored 0–127 Drum Pitch value and automation data untouched. It remaps the value only when firmware reads it for the DSP. Small hooks cover both the live parameter callbacks and pattern/session reload getters. Two unused alignment bytes beside the stock parameter map hold the mode, selected scale, initialization flag, and root; stock callback pointers are not replaced.

The distortion extension redirects the stock DSP distortion call through a compact 56k trampoline. Drum 1 and 2 selectors use stable DSP Y-memory words; Drum 3 and 4 occupy separate packed fields in the hardware-tested Y:$010B word. The ARM wrapper restores the original amount byte before returning, so Shift movement selects a type without changing distortion depth.

The sample-start extension reads the stock sample descriptor at trigger time, advances its base address by the selected proportional displacement, and reduces the remaining boundary by the same amount. Per-drum offset, displacement, and encoder state live in verified-unused DSP X-memory. The normal decay byte is restored before the callback returns.

The reference pitch is C because the Circuit does not store per-sample root-note metadata. Tune the source sample to C for predictable melodic results.

## Legal

No Novation firmware is included in this repository. The MIT license covers only the original patcher, verification tools, and documentation in this project. Novation and Circuit are trademarks of their respective owner. This is an independent community project and is not affiliated with or endorsed by Novation.

See [NOTICE.md](NOTICE.md) for details.

## Credits

Created and hardware-tested by [tracelistener](https://github.com/tracelistener), with reverse-engineering and implementation assistance from OpenAI Codex.
