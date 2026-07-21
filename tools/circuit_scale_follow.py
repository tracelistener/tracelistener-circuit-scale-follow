"""Reference model for the Circuit drum Scale Follow pitch mapping.

The stock Circuit stores each drum pitch as a 7-bit value with 64 as the
sample's original pitch and one semitone per increment.  Scale Follow keeps
that stored 0..127 value intact, but interprets it as a normalized position
within a four-octave span centered on the selected master tonic.

This module is deliberately independent of the firmware patcher.  It is the
executable specification used to validate the ARM implementation.
"""

from __future__ import annotations

from dataclasses import dataclass


SCALE_NAMES = (
    "Natural Minor",
    "Major",
    "Dorian",
    "Phrygian",
    "Mixolydian",
    "Melodic Minor",
    "Harmonic Minor",
    "Bebop Dorian",
    "Blues",
    "Minor Pentatonic",
    "Hungarian Minor",
    "Ukrainian Dorian",
    "Marva",
    "Todi",
    "Whole Tone",
    "Chromatic",
)

# These are the exact 24-bit, two-bits-per-pitch-class masks used by Circuit
# firmware 3592.  A zero two-bit field means that pitch class is in the scale.
SCALE_MASKS = (
    0x00441108,
    0x00221088,
    0x00421108,
    0x00441110,
    0x00421088,
    0x00221108,
    0x00241108,
    0x00421024,
    0x00490124,
    0x00491124,
    0x00240908,
    0x00420908,
    0x00220890,
    0x00240910,
    0x00448888,
    0x00000000,
)


def intervals_from_mask(mask: int) -> tuple[int, ...]:
    """Return the scale's allowed semitone intervals relative to its tonic."""

    return tuple(note for note in range(12) if ((mask >> (note * 2)) & 3) == 0)


SCALE_INTERVALS = tuple(intervals_from_mask(mask) for mask in SCALE_MASKS)


def rounded_ratio(numerator: int, denominator: int) -> int:
    """Round a non-negative rational number to nearest, ties upward."""

    return (numerator + denominator // 2) // denominator


def normalized_degree(value: int, notes_per_octave: int) -> int:
    """Map CC 0..127 to -2..+2 scale octaves, with CC 64 exactly centered."""

    if not 0 <= value <= 127:
        raise ValueError("pitch value must be in the MIDI range 0..127")
    if not 1 <= notes_per_octave <= 12:
        raise ValueError("notes_per_octave must be in the range 1..12")

    degrees_each_side = notes_per_octave * 2
    if value < 64:
        return -rounded_ratio((64 - value) * degrees_each_side, 64)
    return rounded_ratio((value - 64) * degrees_each_side, 63)


def degree_to_semitones(degree: int, intervals: tuple[int, ...]) -> int:
    """Convert a signed scale degree to a signed semitone offset."""

    octave, index = divmod(degree, len(intervals))
    return octave * 12 + intervals[index]


def map_pitch(value: int, scale_index: int, root: int) -> int:
    """Return the stock pitch byte for a C-tuned sample in Scale Follow mode.

    ``root`` is a pitch class (C=0, C#=1, ... B=11).  The result remains in
    the Circuit's stock pitch representation where 64 means no transposition.
    """

    if not 0 <= scale_index < len(SCALE_INTERVALS):
        raise ValueError("scale_index must be in the range 0..15")
    if not 0 <= root <= 11:
        raise ValueError("root must be in the range 0..11")

    intervals = SCALE_INTERVALS[scale_index]
    degree = normalized_degree(value, len(intervals))
    result = 64 + root + degree_to_semitones(degree, intervals)
    return min(127, max(0, result))


@dataclass(frozen=True)
class MappingPoint:
    cc: int
    degree: int
    semitone_offset: int
    output: int


def mapping_table(scale_index: int, root: int) -> tuple[MappingPoint, ...]:
    intervals = SCALE_INTERVALS[scale_index]
    result = []
    for value in range(128):
        degree = normalized_degree(value, len(intervals))
        offset = degree_to_semitones(degree, intervals)
        result.append(MappingPoint(value, degree, offset, map_pitch(value, scale_index, root)))
    return tuple(result)


def self_test() -> None:
    expected_intervals = (
        (0, 2, 3, 5, 7, 8, 10),
        (0, 2, 4, 5, 7, 9, 11),
        (0, 2, 3, 5, 7, 9, 10),
        (0, 1, 3, 5, 7, 8, 10),
        (0, 2, 4, 5, 7, 9, 10),
        (0, 2, 3, 5, 7, 9, 11),
        (0, 2, 3, 5, 7, 8, 11),
        # Circuit's documented "Bebop Dorian" set intentionally omits D.
        (0, 3, 4, 5, 7, 9, 10),
        (0, 3, 5, 6, 7, 10),
        (0, 3, 5, 7, 10),
        (0, 2, 3, 6, 7, 8, 11),
        (0, 2, 3, 6, 7, 9, 10),
        (0, 1, 4, 6, 7, 9, 11),
        (0, 1, 3, 6, 7, 8, 11),
        (0, 2, 4, 6, 8, 10),
        tuple(range(12)),
    )
    assert SCALE_INTERVALS == expected_intervals

    for scale_index, intervals in enumerate(SCALE_INTERVALS):
        count = len(intervals)
        degrees = [normalized_degree(value, count) for value in range(128)]
        assert degrees[0] == -2 * count
        assert degrees[64] == 0
        assert degrees[127] == 2 * count
        assert degrees == sorted(degrees)

        for root in range(12):
            table = mapping_table(scale_index, root)
            assert table[0].output == 40 + root
            assert table[64].output == 64 + root
            assert table[127].output == 88 + root
            assert all(
                left.output <= right.output
                for left, right in zip(table, table[1:])
            )
            for point in table:
                relative_pitch = point.output - (64 + root)
                assert relative_pitch % 12 in intervals


if __name__ == "__main__":
    self_test()
    for index, name in enumerate(SCALE_NAMES):
        intervals = " ".join(f"{value:2d}" for value in SCALE_INTERVALS[index])
        print(f"{index:2d} {name:20s}: {intervals}")
