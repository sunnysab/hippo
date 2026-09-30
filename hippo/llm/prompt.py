"""Prompt assembly for article reading.

Two concerns kept apart: what the article contributes to the context window,
and what each preset action asks for.
"""

from __future__ import annotations

from typing import Any

#: Budget for the article body, in approximate tokens.
#:
#: ponytail: characters / 1.5 is a crude token estimate that ignores
#: tokeniser differences and CJK density; it is deliberately conservative
#: (over-estimating for Chinese) so the request stays inside a 8-16k window.
#: Swap in tiktoken when the model's own limits start to matter.
CONTEXT_TOKEN_BUDGET = 6000

_CHARS_PER_TOKEN = 1.5

#: Appended when the body had to be cut. Told to the model *and* shown to the
#: reader, so nobody wonders why an answer missed the ending.
TRUNCATION_NOTICE = '\n\n[正文过长，已截断，以上为前一部分内容]'

PRESETS: dict[str, str] = {
    'summary': (
        '请用中文为这篇文章写一段结构化摘要。\n'
        '要求：\n'
        '1. 先用一句话说明这篇文章讲什么；\n'
        '2. 再分 3-5 点概括主要内容，每点一行；\n'
        '3. 不要复述原文句子，用你自己的话；\n'
        '4. 不要输出 markdown 标题，直接给内容。'
    ),
    'points': (
        '请用中文提炼这篇文章的要点。\n'
        '要求：\n'
        '1. 输出 5-8 条，每条以 “- ” 开头，一行一条；\n'
        '2. 每条都是可独立理解的完整判断，不要只写关键词；\n'
        '3. 按重要性排序；\n'
        '4. 不要添加原文没有的信息。'
    ),
}

_SYSTEM = (
    '你是一名中文技术文章的阅读助手。回答基于用户提供的文章正文，'
    '不要编造文中没有的事实；如果文章没有提到，就直接说明。'
)


def blocks_to_text(blocks: Any) -> str:
    """Flatten article content blocks into plain text.

    Handles both the structured block list and a bare string, because callers
    differ on which shape they hold.
    """
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ''
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get('text')
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return '\n\n'.join(parts)


def build_context(
    article: dict[str, Any],
    blocks: Any,
    *,
    max_tokens: int = CONTEXT_TOKEN_BUDGET,
) -> tuple[str, bool]:
    """Return ``(context, truncated)`` for the model's system message.

    ``article`` is whatever the article detail endpoint returns; only the
    fields that help the model are included, so a large image list never eats
    the budget.
    """
    title = str(article.get('title') or '').strip()
    author = str(article.get('author') or '').strip()
    body = blocks_to_text(blocks).strip()

    limit = max(int(max_tokens * _CHARS_PER_TOKEN), 500)
    truncated = len(body) > limit
    if truncated:
        body = body[:limit].rstrip()

    header = f'标题：{title}' if title else ''
    if author:
        header = f'{header}\n作者：{author}' if header else f'作者：{author}'

    context = f'{header}\n\n正文：\n{body}' if header else f'正文：\n{body}'
    if truncated:
        context += TRUNCATION_NOTICE
    return context, truncated


def preset_marker(preset: str) -> str:
    """Prefix stored on preset answers so they can be found and reused."""
    return f'[preset:{preset}]'


def strip_preset_marker(content: str) -> str:
    """Drop the ``[preset:x]`` prefix from a stored answer before rendering."""
    if content.startswith('[preset:'):
        end = content.find(']')
        if end != -1:
            return content[end + 1 :].lstrip('\n')
    return content


def system_message(context: str) -> dict[str, str]:
    return {'role': 'system', 'content': f'{_SYSTEM}\n\n{context}'}


__all__ = [
    'CONTEXT_TOKEN_BUDGET',
    'PRESETS',
    'TRUNCATION_NOTICE',
    'blocks_to_text',
    'build_context',
    'preset_marker',
    'strip_preset_marker',
    'system_message',
]
