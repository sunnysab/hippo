"""Subtitles that arrive ready-made.

A podcast episode often ships a transcript (Podcasting 2.0's
``<podcast:transcript>``) and many Bilibili uploaders add CC subtitles. Both are
better than anything we can produce: the timestamps are real rather than
interpolated, and no model has to guess the words — which is why both sit in front
of the ASR in the pipeline.

WebVTT and SRT are close enough to read with one parser: both are cue blocks
separated by blank lines, with a ``-->`` line carrying the timing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Cue text is a line or two; grouping cues keeps the reader from scrolling through
#: one paragraph per subtitle line.
TARGET_PARAGRAPH_CHARS = 200

_TIMING_RE = re.compile(r'(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->')
_TAG_RE = re.compile(r'<[^>]+>')
_SKIP_BLOCKS = ('WEBVTT', 'NOTE', 'STYLE', 'REGION')


@dataclass(frozen=True, slots=True)
class Cue:
    start_seconds: float
    text: str


def _seconds(match: re.Match[str]) -> float:
    hours, minutes, seconds, fraction = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction.ljust(3, '0')) / 1000


def clean_cue_text(text: str) -> str:
    """Drop inline markup and collapse the line breaks a cue may contain."""
    return _TAG_RE.sub('', text).replace('\n', ' ').strip()


def parse_timed_text(payload: str) -> list[Cue]:
    """Read WebVTT or SRT into cues, ignoring anything that is not one."""
    cues: list[Cue] = []
    for block in re.split(r'\n\s*\n', payload.replace('\r\n', '\n')):
        lines = [line for line in block.split('\n') if line.strip()]
        if not lines:
            continue
        if any(lines[0].startswith(prefix) for prefix in _SKIP_BLOCKS):
            continue
        timing_index = next((index for index, line in enumerate(lines) if _TIMING_RE.search(line)), None)
        if timing_index is None:
            continue
        match = _TIMING_RE.search(lines[timing_index])
        if match is None:  # pragma: no cover - guarded by the index search above
            continue
        text = clean_cue_text(' '.join(lines[timing_index + 1 :]))
        if text:
            cues.append(Cue(start_seconds=_seconds(match), text=text))
    return cues


def paragraphs(cues: list[Cue], *, target_chars: int = TARGET_PARAGRAPH_CHARS) -> list[tuple[float, str]]:
    """Group cues into readable paragraphs, keeping each group's first timestamp."""
    grouped: list[tuple[float, str]] = []
    buffer = ''
    start = 0.0
    for cue in cues:
        if buffer and len(buffer) + len(cue.text) > target_chars:
            grouped.append((start, buffer.strip()))
            buffer = ''
        if not buffer:
            start = cue.start_seconds
        buffer = f'{buffer} {cue.text}'.strip()
    if buffer:
        grouped.append((start, buffer.strip()))
    return grouped


def blocks(cues: list[Cue], *, target_chars: int = TARGET_PARAGRAPH_CHARS) -> list[dict[str, object]]:
    return [
        {'type': 'paragraph', 'text': text, 't': round(start, 1)}
        for start, text in paragraphs(cues, target_chars=target_chars)
    ]


def markdown(cues: list[Cue], *, target_chars: int = TARGET_PARAGRAPH_CHARS) -> str:
    """Plain paragraphs, no timestamps: this is what search and export read."""
    return '\n\n'.join(text for _, text in paragraphs(cues, target_chars=target_chars))


__all__ = [
    'TARGET_PARAGRAPH_CHARS',
    'Cue',
    'blocks',
    'clean_cue_text',
    'markdown',
    'paragraphs',
    'parse_timed_text',
]
