# Circuit Scale Follow

Hardware-validated custom firmware for the **original Novation Circuit**, firmware 1.8 build 3592.

## [Open the browser uploader](https://tracelistener.github.io/tracelistener-circuit-scale-follow/)

Use Chrome or Edge. The page loads and verifies the firmware automatically—no Python, command line, or Novation Components import is required.

## Install

1. Back up your Circuit pack in Novation Components, then close Components and all other MIDI software.
2. Connect the Circuit directly by USB.
3. Turn it on while holding **Scales + Note + Velocity**.
4. Open the browser uploader, allow MIDI access, select the Bootloader port, and upload.
5. Leave power and USB connected until the Circuit finishes restarting.

## Features and controls

| Control | Function |
|---|---|
| Drum Pitch | Follows the selected master root and scale across ±2 octaves; sample reference note is C. |
| Shift + Scales | Toggle Scale Follow. It starts enabled after boot. |
| Shift + Macro 3/4 | Move Sample Start for the first/second drum in the active pair. |
| Shift + Macro 5/6 | Select one of seven stock distortion algorithms for the first/second drum. |
| Shift + Macro 7/8 | Select Filter LFO Off or eight speeds: four triangle and four sawtooth. |
| Record + Shift Macro | Record and replay Sample Start, Distortion Type, and Filter LFO movements. |
| Clear + Macro 1–8 clockwise | Perform the stock blue-LED reset and reset the corresponding new control: Scale Follow, Sample Start, Distortion Type, or Filter LFO. |
| Clear + Macro counter-clockwise | Keep the stock red-LED automation-delete behavior. |

Normal Macro movement retains the Circuit's stock Decay, Distortion Amount, and bipolar Filter controls.

## Verified firmware

[Download the current SysEx](docs/firmware/circuit-3592-filter-lfo-shift-automation.syx)

SHA-256:

```text
7ea9affe4c5310a8c3d84abf6c05b1ee35d4ef9ee6d30bb040711a4eb047745f
```

Validated on hardware on 2026-07-31: normal boot, all four drum paths, Scale Follow, Sample Start, independent Distortion Type, Filter LFO, Shift automation playback without a knob nudge, blue-LED Clear resets for every new control, and red-LED automation deletion.

## Compatibility and recovery

- Original Novation Circuit only.
- Requires firmware 1.8 build 3592.
- Not compatible with Circuit Tracks, Circuit Rhythm, or Circuit Mono Station.
- If recovery is needed, enter the same bootloader mode and reinstall stock firmware through [Novation Components](https://components.novationmusic.com/).

Custom firmware always carries risk. Keep reliable power and do not disconnect the Circuit during transfer.

## Source and legal

Patch and verification sources are in [`tools/`](tools/) and [`experimental/`](experimental/). Builders retain the exact stock SHA-256 guard and fixed-size image checks.

The MIT license applies to this project's original code and documentation. Novation and Circuit are trademarks of their respective owner. This independent project is not affiliated with or endorsed by Novation.

Created and hardware-tested by [tracelistener](https://github.com/tracelistener), with reverse-engineering and implementation assistance from OpenAI Codex.
