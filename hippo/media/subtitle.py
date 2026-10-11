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


# ---------------------------------------------------------------------------
# The other direction: content blocks back out as a file someone can take away.
# ---------------------------------------------------------------------------


def timed_paragraphs(blocks: object) -> list[tuple[float, str]]:
    """The transcript's paragraphs and their anchors, out of an article's blocks.

    Blocks are what the reader renders, so the transcript is not stored twice; the
    only thing that matters here is that paragraphs carry ``t``.
    """
    if not isinstance(blocks, list):
        return []
    result: list[tuple[float, str]] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get('type') != 'paragraph':
            continue
        text = str(block.get('text') or '').strip()
        if not text:
            continue
        try:
            start = float(block.get('t') or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        result.append((start, text))
    return result


def format_timestamp(seconds: float, *, comma: bool = False) -> str:
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}' + (',000' if comma else '')


def to_text(blocks: object) -> str:
    return '\n\n'.join(text for _, text in timed_paragraphs(blocks))


def to_markdown(blocks: object, *, title: str | None = None) -> str:
    paragraphs = timed_paragraphs(blocks)
    body = '\n\n'.join(f'**[{format_timestamp(start)[3:]}]** {text}' for start, text in paragraphs)
    return f'# {title}\n\n{body}' if title else body


def to_srt(blocks: object, *, tail_seconds: float = 6.0) -> str:
    """Subtitles from the paragraph anchors.

    Each cue ends where the next one starts, which is exact enough given the
    anchors come from the source (a published transcript) or from chunk boundaries.
    """
    paragraphs = timed_paragraphs(blocks)
    lines: list[str] = []
    for index, (start, text) in enumerate(paragraphs, start=1):
        end = paragraphs[index][0] if index < len(paragraphs) else start + tail_seconds
        lines.append(f'{index}\n{format_timestamp(start, comma=True)} --> {format_timestamp(end, comma=True)}\n{text}\n')
    return '\n'.join(lines)


__all__ = [
    'TARGET_PARAGRAPH_CHARS',
    'Cue',
    'blocks',
    'clean_cue_text',
    'format_timestamp',
    'markdown',
    'paragraphs',
    'parse_timed_text',
    'timed_paragraphs',
    'to_markdown',
    'to_srt',
    'to_text',
]
