"""Full-text coverage, source-neutral numbering and untrusted model boundaries."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from hippo import outline
from hippo.exceptions import ApiError


def answer(start=0, title='Opening', children=None):
    return json.dumps({'sections': [{'title': title, 'start_block': start, 'children': children or []}]})


class SnapshotTest(unittest.TestCase):
    def test_images_and_players_do_not_shift_or_invalidate_text_anchors(self):
        text = [{'type': 'paragraph', 'text': ' First ', 't': 0}, {'type': 'heading', 'text': 'End'}]
        mixed = [{'type': 'video', 'url': 'changed'}, text[0], {'type': 'image', 'image_id': 12}, text[1]]
        self.assertEqual(outline.snapshot('T', text), outline.snapshot('T', mixed))
        self.assertEqual('First', outline.snapshot('T', mixed).blocks[0]['text'])

    def test_title_text_time_and_heading_level_invalidate_the_cache(self):
        content = [{'type': 'paragraph', 'text': 'Body', 't': 0}]
        original = outline.snapshot('T', content).fingerprint
        variants = [
            ('Other', content),
            ('T', [{'type': 'paragraph', 'text': 'New', 't': 0}]),
            ('T', [{'type': 'paragraph', 'text': 'Body', 't': 3}]),
        ]
        for title, blocks in variants:
            self.assertNotEqual(original, outline.snapshot(title, blocks).fingerprint)

    def test_unsupported_and_empty_blocks_are_not_anchors(self):
        source = outline.snapshot(
            'T',
            [
                None,
                {'type': 'paragraph', 'text': ' '},
                {'type': 'unknown', 'text': 'X'},
                {'type': 'code', 'text': 'a()'},
                {'type': 'link', 'text': 'URL'},
                {'type': 'text', 'text': 'Legacy'},
            ],
        )
        self.assertEqual(['a()', 'URL', 'Legacy'], [b['text'] for b in source.blocks])

    def test_json_storage_and_invalid_content(self):
        self.assertEqual(1, len(outline.snapshot('T', '[{"type":"paragraph","text":"Body"}]').blocks))
        for content in (None, '{bad', {}, 'null'):
            self.assertEqual([], outline.snapshot('T', content).blocks)

    def test_whitespace_matches_the_browser_including_bom_and_nel(self):
        source = outline.snapshot(
            'T',
            [
                {'type': 'paragraph', 'text': '\ufeff'},
                {'type': 'paragraph', 'text': '\u0085'},
                {'type': 'paragraph', 'text': '\ufeffBody\ufeff'},
            ],
        )
        self.assertEqual(['\u0085', 'Body'], [block['text'] for block in source.blocks])

    def test_nonfinite_and_negative_timestamps_are_not_exposed(self):
        for time in (float('nan'), float('inf'), -1, True, '10'):
            self.assertIsNone(outline.snapshot('T', [{'type': 'paragraph', 'text': 'Body', 't': time}]).blocks[0]['t'])

    def test_empty_and_oversized_content_are_explicit_errors(self):
        for content, status in (([], 409), ([{'type': 'paragraph', 'text': 'a' * (outline.MAX_TEXT_CHARS + 1)}], 413)):
            with self.assertRaises(ApiError) as ctx:
                outline.validate_size(outline.snapshot('T', content))
            self.assertEqual(status, ctx.exception.status)


class ParseTest(unittest.TestCase):
    def test_valid_fenced_json_is_accepted(self):
        result = outline.parse_outline('```json\n' + answer() + '\n```', {0, 1})
        self.assertEqual('Opening', result[0]['title'])

    def test_invented_reordered_and_cross_chapter_anchors_are_rejected(self):
        invalid = [
            answer(99),
            answer(1),
            answer(title=' '),
            answer(children=[{'title': 'Child', 'start_block': 99}]),
            answer(children=[{'title': 'A', 'start_block': 1}, {'title': 'B', 'start_block': 0}]),
            '{"sections":[{"title":"A","start_block":0},{"title":"B","start_block":0}]}',
            '{"sections":[{"title":"A","start_block":0,"children":[{"title":"C","start_block":2}]},{"title":"B","start_block":1}]}',
            '{"sections":[{"title":"A","start_block":"0"}]}',
            '{}',
            'partial',
            '[]',
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ApiError) as ctx:
                outline.parse_outline(value, {0, 1, 2})
            self.assertEqual('outline.invalid_result', ctx.exception.code)


class GenerateTest(unittest.IsolatedAsyncioTestCase):
    async def test_short_text_uses_one_call_with_the_ending(self):
        source = outline.snapshot(
            'Title', [{'type': 'paragraph', 'text': 'Opening'}, {'type': 'paragraph', 'text': 'ENDING'}]
        )
        with patch.object(outline, 'complete', AsyncMock(return_value=answer())) as call:
            await outline.generate_outline({'model': 'test'}, source)
        self.assertEqual(1, call.await_count)
        self.assertIn('ENDING', call.await_args.args[1][1]['content'])

    async def test_long_text_and_single_long_paragraph_are_never_truncated(self):
        for content in (
            [
                {'type': 'paragraph', 'text': 'a' * 8000},
                {'type': 'paragraph', 'text': 'b' * 8000},
                {'type': 'paragraph', 'text': 'UNIQUE_ENDING'},
            ],
            [{'type': 'paragraph', 'text': 'x' * (outline.CHUNK_CHARS * 2) + 'UNIQUE_ENDING'}],
        ):
            seen = []

            async def fake_complete(provider, messages, seen=seen, **kwargs):
                body = json.loads(messages[1]['content'])['source']
                seen.append(body)
                first = int(body.split(']', 1)[0].removeprefix('['))
                return answer(first)

            with patch.object(outline, 'complete', fake_complete):
                await outline.generate_outline({'model': 'test'}, outline.snapshot('T', content))
            self.assertTrue(any('UNIQUE_ENDING' in text for text in seen))
            self.assertGreaterEqual(len(seen), 3)
            self.assertTrue(all(len(text) <= outline.CHUNK_CHARS for text in seen))

    async def test_a_failed_or_invalid_chunk_never_produces_a_partial_outline(self):
        with patch.object(outline, 'complete', AsyncMock(return_value='partial')), self.assertRaises(ApiError):
            await outline.generate_outline({}, outline.snapshot('T', [{'type': 'paragraph', 'text': 'Body'}]))


if __name__ == '__main__':
    unittest.main()
