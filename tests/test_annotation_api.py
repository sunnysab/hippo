"""Highlight API and repository invariants.

The anchor is a quote plus context, so the things worth testing are the
boundaries: which characters survive into the stored context, who is allowed to
delete, and what gets rejected before it reaches the database.
"""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hippo.api.routers import annotation as annotation_api
from hippo.exceptions import ApiError
from hippo.repositories.annotation import CONTEXT_LENGTH, _row


def _storage(*, deleted: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        annotations=SimpleNamespace(
            list_for_article=AsyncMock(return_value=[]),
            create=AsyncMock(return_value={'id': 1}),
            delete=AsyncMock(return_value=deleted),
        ),
        transaction=lambda: _NullTransaction(),
    )


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _user(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username='reader', role='user')


class RoundTripTest(unittest.TestCase):
    def test_a_stored_row_comes_back_with_the_same_anchor(self) -> None:
        now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        stored = {
            'id': 9,
            'article_pk': 42,
            'quote': '一段被划出来的话',
            'prefix': '前文',
            'suffix': '后文',
            'note': '',
            'color': 'default',
            'created_at': now,
        }

        result = _row(stored)

        self.assertEqual('一段被划出来的话', result['quote'])
        self.assertEqual('前文', result['prefix'])
        self.assertEqual('后文', result['suffix'])
        # article_pk is the internal key; the API speaks article_id.
        self.assertEqual(42, result['article_id'])
        self.assertEqual(now.isoformat(), result['created_at'])


class AnnotationApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_empty_quote_is_rejected(self) -> None:
        storage = _storage()
        with patch.object(annotation_api, '_ensure_article', AsyncMock()), self.assertRaises(ApiError):
            await annotation_api.create_annotation(
                1, body={'quote': '   '}, user=_user(), storage=storage
            )
        storage.annotations.create.assert_not_awaited()

    async def test_an_overlong_quote_is_rejected(self) -> None:
        storage = _storage()
        with patch.object(annotation_api, '_ensure_article', AsyncMock()), self.assertRaises(ApiError):
            await annotation_api.create_annotation(
                1,
                body={'quote': 'x' * (annotation_api.MAX_QUOTE_LENGTH + 1)},
                user=_user(),
                storage=storage,
            )
        storage.annotations.create.assert_not_awaited()

    async def test_context_is_capped_at_the_shared_length(self) -> None:
        storage = _storage()
        with patch.object(annotation_api, '_ensure_article', AsyncMock()):
            await annotation_api.create_annotation(
                5,
                body={
                    'quote': 'q',
                    'prefix': 'p' * (CONTEXT_LENGTH * 3),
                    'suffix': 's' * (CONTEXT_LENGTH * 3),
                },
                user=_user(7),
                storage=storage,
            )

        kwargs = storage.annotations.create.await_args.kwargs
        self.assertEqual(CONTEXT_LENGTH, len(kwargs['prefix']))
        self.assertEqual(CONTEXT_LENGTH, len(kwargs['suffix']))
        self.assertEqual(7, kwargs['user_id'])
        self.assertEqual(5, kwargs['article_pk'])

    async def test_an_unknown_colour_is_rejected(self) -> None:
        storage = _storage()
        with patch.object(annotation_api, '_ensure_article', AsyncMock()), self.assertRaises(ApiError):
            await annotation_api.create_annotation(
                1, body={'quote': 'q', 'color': 'chartreuse'}, user=_user(), storage=storage
            )

    async def test_deleting_someone_elses_highlight_is_a_404(self) -> None:
        storage = _storage(deleted=False)
        with patch.object(annotation_api, '_ensure_article', AsyncMock()), self.assertRaises(ApiError) as ctx:
            await annotation_api.delete_annotation(1, 99, user=_user(7), storage=storage)
        self.assertEqual(404, ctx.exception.status)
        # The ownership predicate is the delete itself, not a separate read.
        storage.annotations.delete.assert_awaited_once_with(99, 7)

    async def test_a_missing_article_is_a_404_before_any_write(self) -> None:
        storage = _storage()
        with patch.object(
            annotation_api, '_ensure_article', AsyncMock(side_effect=ApiError('not found', status=404))
        ), self.assertRaises(ApiError) as ctx:
            await annotation_api.create_annotation(
                1, body={'quote': 'q'}, user=_user(), storage=storage
            )
        self.assertEqual(404, ctx.exception.status)
        storage.annotations.create.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
