# Changelog

## 0.1.0 — 2026-07-21

- Add scale-quantized Drum Pitch over a ±2-octave range.
- Follow all 16 stock master scales and all 12 root notes.
- Add Shift + Scales mode toggle with default-on cold-boot initialization.
- Cover all four live Drum Pitch callbacks and all four pattern/session reload paths.
- Preserve stock pitch behavior while the mode is off.
- Add deterministic build checks, exhaustive reference-model tests, Thumb emulation, and a guarded SysEx sender.
- Hardware-validate cold boot, scale changes, root changes, mode toggle, live pitch control, and reboot persistence on an original Circuit.
