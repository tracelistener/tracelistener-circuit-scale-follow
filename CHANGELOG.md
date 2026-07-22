# Changelog

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
