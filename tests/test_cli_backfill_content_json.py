from __future__ import annotations

import unittest
from unittest.mock import patch

from hippo.cli import backfill_content_json


class _FakeCursor:
    def __init__(self, conn: '_FakeConn') -> None:
        self._conn = conn
        self._result: list[tuple] = []
        self._sql: str = ''

    def __enter__(self) -> '_FakeCursor':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        return None

    def execute(self, sql: str, params=None) -> None:  # type: ignore[no-untyped-def]
        self._sql = sql
        normalized_sql = ' '.join(sql.split())
        if normalized_sql.startswith(
            'SELECT MIN(article_pk), MAX(article_pk)'
        ):
            self._result = [(1, 10)]
            return
        if normalized_sql.startswith(
            'SELECT article_pk FROM article_content'
        ):
            last_article_pk = params[0] if params else 0
            self._result = self._conn.select_all_pks(last_article_pk)
            return
        if (
            normalized_sql.startswith(
                'SELECT article_pk, content_markdown, content_json FROM article_content'
            )
            and 'article_pk = ANY(%s)' in normalized_sql
        ):
            article_pks = [int(value) for value in params[0]]
            self._result = self._conn.select_article_content_by_pks(article_pks)
            return
        if normalized_sql.startswith(
            'SELECT article_pk, content_markdown, content_json FROM article_content'
        ):
            last_article_pk = params[0]
            limit = params[-1]
            self._result = self._conn.select_article_content(last_article_pk, limit)
            return
        if normalized_sql.startswith(
            'SELECT article_pk, id, orig_url FROM article_images WHERE article_pk = ANY(%s)'
        ):
            article_pks = [int(value) for value in params[0]]
            self._result = self._conn.select_article_images(article_pks)
            return
        raise AssertionError(f'Unexpected SQL: {normalized_sql}')

    def fetchall(self) -> list[tuple]:
        return list(self._result)

    def fetchone(self) -> tuple | None:
        return self._result[0] if self._result else None

    def executemany(self, sql: str, params_seq) -> None:  # type: ignore[no-untyped-def]
        self._conn.executed_updates.extend(list(params_seq))


class _FakeConn:
    def __init__(self, *, article_rows_map: dict | None = None) -> None:
        self._article_content_map = {
            1: (1, 'one', [{'type': 'paragraph', 'text': 'old'}]),
            2: (2, 'two', None),
            3: (3, 'three', [{'type': 'paragraph', 'text': 'three'}]),
        }
        self.article_rows = article_rows_map or {
            0: [
                (1, 'one', [{'type': 'paragraph', 'text': 'old'}]),
                (2, 'two', None),
            ],
            2: [
                (3, 'three', [{'type': 'paragraph', 'text': 'three'}]),
            ],
            3: [],
        }
        self.rollback_count = 0
        self.executed_updates: list[tuple] = []

    def cursor(self) -> '_FakeCursor':
        return _FakeCursor(self)

    def rollback(self) -> None:
        self.rollback_count += 1

    def select_all_pks(self, last_article_pk: int) -> list[tuple]:
        return [(pk,) for pk in sorted(self._article_content_map) if pk > last_article_pk]

    def select_article_content_by_pks(self, article_pks: list[int]) -> list[tuple]:
        return [
            self._article_content_map[pk]
            for pk in sorted(article_pks)
            if pk in self._article_content_map
        ]

    def select_article_content(self, last_article_pk: int, limit: int) -> list[tuple]:
        return list(self.article_rows.get(last_article_pk, []))[:limit]

    def select_article_images(self, article_pks: list[int]) -> list[tuple]:
        return []


class _FakeTransaction:
    def __enter__(self) -> '_FakeTransaction':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        return None


class _FakeStorage:
    instances: list['_FakeStorage'] = []

    def __init__(self, dsn: str, *, auto_init: bool = False) -> None:
        self.dsn = dsn
        self.auto_init = auto_init
        self.conn = _FakeConn()
        _FakeStorage.instances.append(self)

    def __enter__(self) -> '_FakeStorage':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        return None

    def rollback(self) -> None:
        self.conn.rollback()

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


def _fake_parse_markdown_blocks(markdown: str) -> tuple[None, None, list[dict], str]:
    return None, None, [{'type': 'paragraph', 'text': markdown}], markdown


def _fake_attach_image_block_metadata(
    blocks: list[dict],
    *,
    resolve_url,
    image_id_by_url=None,
) -> list[dict]:
    return blocks


class BackfillContentJsonCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        _FakeStorage.instances.clear()

    def test_dry_run_processes_rows_in_batches_and_reports_progress(self) -> None:
        outputs: list[str] = []

        with (
            patch('hippo.cli.PostgresStorage', _FakeStorage),
            patch('hippo.cli._parse_markdown_blocks', side_effect=_fake_parse_markdown_blocks),
            patch(
                'hippo.cli._attach_image_block_metadata',
                side_effect=_fake_attach_image_block_metadata,
            ),
            patch('hippo.cli.typer.echo', side_effect=outputs.append),
        ):
            backfill_content_json(
                pg_dsn='postgresql://example',
                article_pk=None,
                batch_size=2,
                limit=3,
                dry_run=True,
                workers=1,
            )

        self.assertEqual(
            outputs,
            [
                'Dry run progress: processed 2 articles, would update 2.',
                'Dry run progress: processed 3 articles, would update 2.',
                'Would backfill content_json for 2 articles.',
            ],
        )
        self.assertEqual(len(_FakeStorage.instances), 1)
        self.assertEqual(_FakeStorage.instances[0].conn.rollback_count, 4)
        self.assertEqual(_FakeStorage.instances[0].conn.executed_updates, [])

    def test_multi_worker_parallel_execution(self) -> None:
        outputs: list[str] = []

        with (
            patch('hippo.cli.PostgresStorage', _FakeStorage),
            patch('hippo.cli._parse_markdown_blocks', side_effect=_fake_parse_markdown_blocks),
            patch(
                'hippo.cli._attach_image_block_metadata',
                side_effect=_fake_attach_image_block_metadata,
            ),
            patch('hippo.cli.typer.echo', side_effect=outputs.append),
        ):
            backfill_content_json(
                pg_dsn='postgresql://example',
                article_pk=None,
                batch_size=100,
                limit=None,
                dry_run=True,
                workers=2,
            )

        self.assertIn(
            'Would backfill content_json for 2 articles across 2 workers.',
            outputs,
        )

    def test_single_article_skips_parallel(self) -> None:
        outputs: list[str] = []

        with (
            patch('hippo.cli.PostgresStorage', _FakeStorage),
            patch('hippo.cli._parse_markdown_blocks', side_effect=_fake_parse_markdown_blocks),
            patch(
                'hippo.cli._attach_image_block_metadata',
                side_effect=_fake_attach_image_block_metadata,
            ),
            patch('hippo.cli.typer.echo', side_effect=outputs.append),
        ):
            backfill_content_json(
                pg_dsn='postgresql://example',
                article_pk=1,
                batch_size=100,
                limit=None,
                dry_run=False,
                workers=4,
            )

        self.assertIn('Backfilled content_json for 1 articles.', outputs)


if __name__ == '__main__':
    unittest.main()
