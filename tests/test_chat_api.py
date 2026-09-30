"""Prompt assembly and streaming persistence.

The behaviour that matters most: an interrupted stream must not leave a partial
assistant turn behind, because every later request replays the history.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hippo.api.routers import chat as chat_api
from hippo.exceptions import ApiError
from hippo.llm import prompt as prompt_module


class BuildContextTest(unittest.TestCase):
    def test_blocks_are_flattened_into_paragraphs(self) -> None:
        blocks = [
            {'type': 'text', 'text': '第一段'},
            {'type': 'image', 'image_id': 1},
            {'type': 'text', 'text': '第二段'},
        ]
        self.assertEqual('第一段\n\n第二段', prompt_module.blocks_to_text(blocks))

    def test_a_plain_string_passes_through(self) -> None:
        self.assertEqual('hello', prompt_module.blocks_to_text('hello'))

    def test_the_body_is_truncated_with_a_notice(self) -> None:
        blocks = [{'type': 'text', 'text': 'x' * 20_000}]
        context, truncated = prompt_module.build_context({'title': 'T'}, blocks)

        self.assertTrue(truncated)
        self.assertIn(prompt_module.TRUNCATION_NOTICE, context)
        self.assertIn('标题：T', context)

    def test_short_content_is_untouched(self) -> None:
        context, truncated = prompt_module.build_context({'title': 'T'}, [{'text': 'short'}])
        self.assertFalse(truncated)
        self.assertIn('short', context)

    def test_author_is_included_when_present(self) -> None:
        context, _ = prompt_module.build_context({'title': 'T', 'author': 'A'}, [{'text': 'b'}])
        self.assertIn('作者：A', context)


class PresetMarkerTest(unittest.TestCase):
    def test_a_marker_round_trips(self) -> None:
        stored = f'{prompt_module.preset_marker("summary")}\n摘要内容'
        self.assertEqual('摘要内容', prompt_module.strip_preset_marker(stored))

    def test_an_unmarked_answer_is_returned_as_is(self) -> None:
        self.assertEqual('普通回答', prompt_module.strip_preset_marker('普通回答'))

    def test_every_declared_preset_has_an_instruction(self) -> None:
        for name in ('summary', 'points'):
            self.assertIn(name, prompt_module.PRESETS)
            self.assertTrue(prompt_module.PRESETS[name].strip())


class StreamTest(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, *, deltas: list[str], preset: str = '') -> tuple[list[dict], list]:
        stored: list[tuple] = []

        async def fake_stream(provider, messages, **kwargs):
            for delta in deltas:
                yield delta

        class _Chat:
            async def append_message(self, session_id, role, content):
                stored.append((session_id, role, content))
                return {'id': 99}

        storage = SimpleNamespace(
            transaction=lambda: _NullTransaction(),
            chat=_Chat(),
        )

        def fake_open_storage():
            return _OpenStorage(storage)

        events: list[dict] = []
        with patch.object(chat_api, 'stream_chat', fake_stream), patch.object(
            chat_api, 'open_storage', fake_open_storage
        ):
            async for chunk in chat_api._stream(1, {'id': 1}, [], preset, False):
                events.append(json.loads(chunk.removeprefix('data: ')))

        return events, stored

    async def test_a_completed_answer_is_stored_once(self) -> None:
        events, stored = await self._collect(deltas=['he', 'llo'])

        self.assertEqual('hello', ''.join(e['delta'] for e in events if 'delta' in e))
        self.assertTrue(events[-1]['done'])
        self.assertEqual(1, len(stored))
        self.assertEqual(('assistant', 'hello'), (stored[0][1], stored[0][2]))

    async def test_an_interrupted_answer_is_not_stored(self) -> None:
        async def exploding_stream(provider, messages, **kwargs):
            yield 'partial'
            raise ApiError('upstream died', status=502)

        stored: list[tuple] = []

        class _Chat:
            async def append_message(self, session_id, role, content):
                stored.append((session_id, role, content))
                return {'id': 99}

        storage = SimpleNamespace(transaction=lambda: _NullTransaction(), chat=_Chat())

        events: list[dict] = []
        with patch.object(chat_api, 'stream_chat', exploding_stream), patch.object(
            chat_api, 'open_storage', lambda: _OpenStorage(storage)
        ):
            async for chunk in chat_api._stream(1, {'id': 1}, [], '', False):
                events.append(json.loads(chunk.removeprefix('data: ')))

        # The client saw the error, and nothing reached the database.
        self.assertEqual('upstream died', events[-1]['error'])
        self.assertEqual([], stored)

    async def test_a_preset_answer_is_stored_with_its_marker(self) -> None:
        _, stored = await self._collect(deltas=['sum'], preset='summary')
        self.assertTrue(stored[0][2].startswith('[preset:summary]'))


class ReplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_cached_answer_streams_without_touching_the_model(self) -> None:
        events = [json.loads(chunk.removeprefix('data: ')) async for chunk in chat_api._replay('cached text', 5)]

        self.assertEqual('cached text', events[0]['delta'])
        self.assertTrue(events[1]['cached'])
        self.assertEqual(5, events[1]['message_id'])


class PresetGuardTest(unittest.TestCase):
    def test_an_unknown_preset_is_rejected(self) -> None:
        with self.assertRaises(ApiError):
            chat_api._require_preset('translate-to-klingon')


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _OpenStorage:
    def __init__(self, storage) -> None:
        self._storage = storage

    async def __aenter__(self):
        return self._storage

    async def __aexit__(self, *exc):
        return False


class ContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_free_chat_says_there_is_no_article(self) -> None:
        context, truncated = await chat_api._article_context(SimpleNamespace(), 1, {'article_id': None})
        self.assertIn('自由对话', context)
        self.assertFalse(truncated)


if __name__ == '__main__':
    unittest.main()
