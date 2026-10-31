"""User-specific, non-destructive ASR text projection."""

from __future__ import annotations

import re
from typing import Any


def effective_rules(rules: list[dict[str, Any]], biz: str | None) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for rule in rules:
        scope = rule['account_biz']
        if not rule['enabled'] or (scope and biz not in scope):
            continue
        source = rule['source_text']
        if source not in selected or scope:
            selected[source] = rule
    return list(selected.values())


def _utf16_length(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


# shortcut: complex Markdown links can protect the rest of their line; use a parser for precise nesting.
_PROTECTED = re.compile(
    r'(?m)^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]{0,3}(?P=fence)[ \t]*(?:\n|$)'
    r'|(?m:^[ \t]{0,3}(?:`{3,}|~{3,})[^\n]*(?:\n|$))[\s\S]*\Z'
    r'|(?P<ticks>`+)[^`]*?(?P=ticks)'
    r'|`+[^\n]*'
    r'|!?\[(?:\\.|[^\[\]\\\n]|\[[^\[\]\n]*\])*\]\([^\n]*\)'
    r'|!?\[(?:\\.|[^\[\]\\\n]|\[[^\[\]\n]*\])*\]\([^\n]*'
    r'|!?\[(?:\\.|[^\[\]\\\n]|\[[^\[\]\n]*\])*\]\[(?:\\.|[^\]\\\n])*\]'
    r'|<[^\n<>]+>'
    r'|(?i:https?://|www\.)[^\s<>\[\]`，。！？；：、”’》】）]+'
    r'|(?<![\d:])\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?![\d:])'
    r'|\\[^\n]'
    r'|[*_~]+'
    r'|(?m:^[ \t]{0,3}(?:#{1,6}[ \t]+|>[ \t]*|[-+][ \t]+|\d+[.)][ \t]+))',
    re.DOTALL,
)


def apply_text(text: str, rules: list[dict[str, Any]]) -> tuple[str, list[dict[str, int]]]:
    if not rules:
        return text, []
    replacements = {rule['source_text']: rule['replacement_text'] for rule in rules}
    pattern = re.compile('|'.join(re.escape(source) for source in sorted(replacements, key=len, reverse=True)))
    pieces: list[str] = []
    spans: list[dict[str, int]] = []
    last = original_offset = output_offset = 0
    protected = list(_PROTECTED.finditer(text))
    boundaries = [(0 if i == 0 else protected[i - 1].end(), match.start()) for i, match in enumerate(protected)]
    boundaries.append((protected[-1].end() if protected else 0, len(text)))
    matches = (match for start, end in boundaries for match in pattern.finditer(text, start, end))
    for match in matches:
        unchanged = text[last : match.start()]
        pieces.append(unchanged)
        unchanged_length = _utf16_length(unchanged)
        original_offset += unchanged_length
        output_offset += unchanged_length
        replacement = replacements[match.group()]
        original_end = original_offset + _utf16_length(match.group())
        output_end = output_offset + _utf16_length(replacement)
        spans.append(
            {'original_start': original_offset, 'original_end': original_end, 'start': output_offset, 'end': output_end}
        )
        pieces.append(replacement)
        original_offset, output_offset, last = original_end, output_end, match.end()
    pieces.append(text[last:])
    return ''.join(pieces), spans


def project_blocks(blocks: list[dict[str, Any]], rules: list[dict[str, Any]], biz: str | None) -> list[dict[str, Any]]:
    applicable = effective_rules(rules, biz)
    projected = []
    for block in blocks:
        if not isinstance(block, dict):
            projected.append(block)
            continue
        copy = dict(block)
        if (
            block.get('type') == 'paragraph'
            and block.get('transcript_source') == 'asr'
            and isinstance(block.get('text'), str)
        ):
            original = block['text']
            copy['text'], copy['correction_spans'] = apply_text(original, applicable)
            copy['original_text'] = original
        projected.append(copy)
    return projected
