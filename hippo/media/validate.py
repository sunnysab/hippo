"""Deciding whether a transcript is believable.

Every failure mode measured on the real backend (PLAN §5.3) produces a string
that *looks* like a transcript, so the check has to be on shape rather than on
wording:

* A 20 s trailing chunk made the decoder loop and answer with 2048 ``!``
  characters — one character owning the whole text.
* A chunk longer than the generation budget stops mid-sentence and silently drops
  the rest of the audio — the output lands right under the cap.

A chunk that is music or silence legitimately has nothing to say, which is not a
failure: that case is reported as ``silent`` so the caller can skip it instead of
retrying it three times.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Normal Chinese speech lands near 6.2 characters per second. The window exists
#: to catch a repetition loop (measured at 102/s), not to grade the model, so it
#: is deliberately wide.
MIN_CHARS_PER_SECOND = 0.5
MAX_CHARS_PER_SECOND = 15.0

#: One character owning more than half the text is the ``!!!!`` signature.
MAX_REPEAT_RATIO = 0.5

#: Shorter than this and there was nothing to transcribe.
MIN_CHARS = 10

#: Measured against the real model: 2048 generated tokens produced 3367 Chinese
#: characters before stopping at the cap. Used only to recognise a hitting-the-cap
#: case, and deliberately rounded down so the warning errs toward firing early.
#:
#: Latin script needs far fewer tokens for the same number of characters (~4 per
#: token against ~1.6 for Chinese), so one ratio cannot serve both: a 300 s English
#: chunk that transcribed perfectly well came out at 3500 characters and was
#: rejected as truncated. Which ratio applies is decided from the text itself —
#: the configured language is not a reliable guide, since English audio transcribed
#: under a ``zh`` setting still yields English text.
CHARS_PER_TOKEN = 1.6
CHARS_PER_TOKEN_LATIN = 4.0
#: How close to the cap counts as "probably truncated".
CAP_RATIO = 0.9


@dataclass(frozen=True, slots=True)
class Verdict:
    """What to do with one chunk's transcript."""

    ok: bool
    reason: str | None = None
    #: Nothing was said in this chunk. Not an error, and not worth retrying.
    silent: bool = False


def _chars_per_token(text: str) -> float:
    """Pick the ratio that matches the script the transcript actually came out in."""
    cjk = sum(1 for character in text if '\u4e00' <= character <= '\u9fff')
    return CHARS_PER_TOKEN if cjk * 2 >= len(text) else CHARS_PER_TOKEN_LATIN


def _peak_repeat_ratio(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for character in text:
        counts[character] = counts.get(character, 0) + 1
    return max(counts.values()) / len(text)


def check(text: str, duration_seconds: float, *, max_new_tokens: int | None = None) -> Verdict:
    """Judge one chunk's transcript.

    ``max_new_tokens`` applies to the LLM-style backends only; pass ``None`` for a
    backend that has no generation cap.
    """
    stripped = (text or '').strip()
    if duration_seconds <= 0:
        return Verdict(ok=False, reason=f'切片时长为 {duration_seconds}')
    if len(stripped) < MIN_CHARS:
        return Verdict(ok=False, silent=True, reason='切片内没有可转写的内容')

    rate = len(stripped) / duration_seconds
    if rate > MAX_CHARS_PER_SECOND:
        return Verdict(ok=False, reason=f'字符/秒异常（{rate:.1f}），疑似解码循环')

    repeat_ratio = _peak_repeat_ratio(stripped)
    if repeat_ratio > MAX_REPEAT_RATIO:
        return Verdict(ok=False, reason=f'单个字符占比 {repeat_ratio:.0%}，疑似解码循环')

    if max_new_tokens:
        cap = max_new_tokens * _chars_per_token(stripped) * CAP_RATIO
        if len(stripped) >= cap:
            return Verdict(ok=False, reason=f'输出 {len(stripped)} 字接近生成长度上限，疑似被截断')

    return Verdict(ok=True)


__all__ = [
    'CAP_RATIO',
    'CHARS_PER_TOKEN',
    'CHARS_PER_TOKEN_LATIN',
    'MAX_CHARS_PER_SECOND',
    'MAX_REPEAT_RATIO',
    'MIN_CHARS',
    'MIN_CHARS_PER_SECOND',
    'Verdict',
    'check',
]
