"""Opt-in correction repository checks against an isolated PostgreSQL schema."""

import asyncio
import os
import re
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg import sql

from hippo.api.routers.correction import create_correction
from hippo.api.schemas.correction import CorrectionInput
from hippo.exceptions import ApiError
from hippo.repositories.correction import CorrectionRepository

DSN = os.environ.get('HIPPO_TEST_PG_DSN')


@unittest.skipUnless(DSN, 'HIPPO_TEST_PG_DSN must point at an isolated PostgreSQL test server')
class CorrectionPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await psycopg.AsyncConnection.connect(DSN, autocommit=True)
        self.schema = f'hippo_correction_test_{uuid.uuid4().hex}'
        await self.conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addAsyncCleanup(self.cleanup)
        await self.conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        await self.conn.execute('CREATE TABLE users (id integer PRIMARY KEY)')
        await self.conn.execute("""CREATE TABLE subscription (
            user_id integer NOT NULL REFERENCES users(id), biz text NOT NULL,
            is_disabled boolean NOT NULL DEFAULT false, PRIMARY KEY (user_id, biz)
        )""")
        schema = (Path(__file__).resolve().parent.parent / 'schema/postgres.sql').read_text()
        table = re.search(r'CREATE TABLE IF NOT EXISTS correction_rule \(.*?\n\);', schema, re.DOTALL)
        index = re.search(r'CREATE INDEX IF NOT EXISTS idx_correction_rule_user[^;]*;', schema)
        self.assertIsNotNone(table)
        self.assertIsNotNone(index)
        for _ in range(2):
            await self.conn.execute(table.group())
            await self.conn.execute(index.group())
        await self.conn.execute('INSERT INTO users VALUES (7), (8)')
        await self.conn.execute("INSERT INTO subscription VALUES (7, 'one', false), (7, 'off', true)")
        await self.conn.set_autocommit(False)

    async def cleanup(self):
        await self.conn.rollback()
        await self.conn.set_autocommit(True)
        await self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        await self.conn.close()

    async def other_connection(self):
        conn = await psycopg.AsyncConnection.connect(DSN)
        await conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
        await conn.commit()
        return conn

    async def test_repository_isolation_and_disabled_scope_lock(self):
        repo = CorrectionRepository(self.conn)
        self.assertTrue(await repo.valid_scope(7, ['one']))
        self.assertFalse(await repo.valid_scope(7, ['off']))
        self.assertFalse(await repo.valid_scope(8, ['one']))
        data = {'source_text': 'a', 'replacement_text': 'b', 'account_biz': ['one'], 'enabled': True}
        created = await repo.create(7, data)
        self.assertEqual([created], await repo.list_for_user(7))
        self.assertEqual([], await repo.list_for_user(8))
        self.assertFalse(await repo.delete(8, created['id']))

        other = await self.other_connection()
        try:
            await other.execute("SET LOCAL lock_timeout = '100ms'")
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                await other.execute("UPDATE subscription SET is_disabled = true WHERE user_id = 7 AND biz = 'one'")
        finally:
            await other.rollback()
            await other.close()

    async def test_user_lock_serializes_overlapping_rule_creation(self):
        first = await self.other_connection()
        second = await self.other_connection()
        locked = asyncio.Event()
        second_started = asyncio.Event()

        class FirstRepository(CorrectionRepository):
            async def lock_user(self, user_id):
                await super().lock_user(user_id)
                locked.set()
                await second_started.wait()

        async def attempt(conn, repo, wait_for_first=False):
            if wait_for_first:
                await locked.wait()
                second_started.set()
            storage = SimpleNamespace(corrections=repo, transaction=conn.transaction)
            body = CorrectionInput(source_text='a', replacement_text='b', account_biz=['one'], enabled=True)
            try:
                await create_correction(body, SimpleNamespace(id=7), storage)
                return 201
            except ApiError as exc:
                return exc.status

        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    attempt(first, FirstRepository(first)),
                    attempt(second, CorrectionRepository(second), True),
                ),
                timeout=10,
            )
            self.assertEqual([201, 409], sorted(results))
            self.assertEqual(1, len(await CorrectionRepository(self.conn).list_for_user(7)))
        finally:
            await first.close()
            await second.close()
