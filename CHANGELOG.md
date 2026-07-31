# Changelog

## 0.5.0 — 2026-07-31

- Add the per-drum Filter LFO with four triangle and four sawtooth speeds.
- Record and replay hidden Shift controls through the stock drum automation lanes.
- Remove the one-event Filter-LFO recording lag and playback knob-nudge requirement.
- Make the stock Clear + Macro clockwise gesture reset Sample Start, Distortion Type, or Filter LFO for the selected drum while retaining its blue LED.
- Preserve Clear + Macro counter-clockwise automation deletion and its red LED.
- Replace the public install paths with one browser uploader that loads and verifies the hardware-tested firmware automatically.

## 0.4.2 — 2026-07-26

- Fix Synth 1 patch changes overwriting drum distortion algorithm selections.
- Move the four selectors from live Synth 1 voice state at DSP Y:$0109–$010B to independent X:$1E65–$1E68 words.
- Remove the packed Drum 3/4 representation; every drum now owns a complete word.
- Preserve normal Macro 5/6 amount control, Scale Follow, Sample Start, and cold-boot behavior.
- Add emulated regression coverage for every drum, both directions, repeated events, direction reversal, invalid state, and Synth 1 isolation.
- Hardware-validate Synth 1 patch switching, all four independent selectors, normal distortion amount, Scale Follow, Sample Start, and cold boot.
- Add focused troubleshooting for a Windows MIDI backend failure on the first SysEx message.

## 0.4.1 — 2026-07-22

- Require a one-time 10-second cold power removal after firmware upload.
- Explain that the bootloader's warm restart can retain the previous Scale Follow RAM state.
- Add the cold-boot instruction to the GUI completion dialog, README, and Windows walkthrough.
- Keep the v0.4.0 firmware image and SysEx bytes unchanged.

## 0.4.0 — 2026-07-22

- Add Shift + Macro 3/4 per-drum sample-start control.
- Provide approximately 64 evenly spaced encoder positions across each sample.
- Track relative encoder events independently so direction reversals remain smooth.
- Preserve normal decay values and non-Shift Macro 3/4 behavior.
- Recompute the proportional displacement when a different sample is selected.
- Apply offsets only on new triggers so sounding voices are not moved abruptly.
- Reach the sample beginning and final safe sample position with bounded descriptor arithmetic.
- Hardware-validate the full sweep, normal decay, and independent Drum 1/2 state.
- Extend deterministic build and emulation checks across first, consecutive, reversed, clamped, and sample-change events.

## 0.3.0 — 2026-07-22

- Add Shift + Macro 5/6 selection of all seven stock DSP distortion algorithms.
- Keep independent algorithm state for all four drum parts.
- Preserve normal Macro 5/6 distortion-amount behavior and restore the stored amount during selection gestures.
- Pack Drum 3/4 selectors into the hardware-validated DSP Y:$010B word for stable operation.
- Extend deterministic build checks across the added ARM wrappers, DSP trampoline, hooks, and changed-byte boundaries.
- Hardware-validate forward and reverse selection, all four independent drum states, stable Drum 4 operation, normal amount control, and Scale Follow regression behavior.

## 0.2.0 — 2026-07-21

- Add a self-contained Windows GUI for building and uploading the mod.
- Require the exact verified stock 3592 SysEx before firmware can be generated.
- Recheck the deterministic patched hash before enabling upload.
- Always write a stock recovery copy beside the generated firmware.
- Use a fixed conservative 22 ms transfer interval and a final confirmation naming the selected MIDI port.
- Prevent the GUI from closing while a firmware transfer is active.
- Add reproducible PyInstaller packaging and Windows usage documentation.

## 0.1.0 — 2026-07-21

- Add scale-quantized Drum Pitch over a ±2-octave range.
- Follow all 16 stock master scales and all 12 root notes.
- Add Shift + Scales mode toggle with default-on cold-boot initialization.
- Cover all four live Drum Pitch callbacks and all four pattern/session reload paths.
- Preserve stock pitch behavior while the mode is off.
- Add deterministic build checks, exhaustive reference-model tests, Thumb emulation, and a guarded SysEx sender.
- Hardware-validate cold boot, scale changes, root changes, mode toggle, live pitch control, and reboot persistence on an original Circuit.
