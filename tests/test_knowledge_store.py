"""Knowledge document storage, with the object store faked out.

What is worth pinning down: the real SHA-256 and length come from the bytes and
not from the caller, the file is handed to the SDK as a *path* and streamed in
fixed blocks, every attempt gets its own key built only from internal
identifiers, a file that changes mid-upload fails, failures clean up exactly
their own object, and the delete helper touches exactly one key.
"""

import hashlib
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from boto3.s3.transfer import TransferConfig

from hippo.knowledge import store as store_module
from hippo.knowledge.parse import MAX_FILE_BYTES
from hippo.knowledge.store import (
    DocumentStoreError,
    build_document_key,
    delete_document_object,
    download_document,
    store_document,
)
from hippo.s3 import S3Config

_CONFIG = S3Config(
    endpoint='http://minio.test',
    bucket='knowledge',
    access_key='key',
    secret_key='secret',
    region='us-east-1',
    prefix='mp/image/',
)

BASE_ID = '11111111-1111-4111-8111-111111111111'
DOC_ID = '22222222-2222-4222-8222-222222222222'
TOKEN = '33333333-3333-4333-8333-333333333333'
OTHER_TOKEN = '44444444-4444-4444-8444-444444444444'

READ_CHUNK = 1 << 16

DOWNLOAD_KEY = f'knowledge/{BASE_ID}/{DOC_ID}/{TOKEN}.pdf'


class _FakeBody:
    """A bounded stand-in for the SDK body, recording every read request.

    ``blocks`` are returned one per ``read`` call, so short, long and altered
    streams are written as small payloads instead of large buffers.
    """

    def __init__(
        self,
        blocks: list[bytes],
        *,
        read_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.blocks = list(blocks)
        self.read_error = read_error
        self.close_error = close_error
        self.requests: list[int] = []
        self.reads = 0
        self.closed = 0

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        if self.blocks:
            return self.blocks.pop(0)
        return b''

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class _FailingHandle:
    """A real file handle whose ``write``/``flush``/``close`` can be made to fail."""

    def __init__(self, raw: Any, *, fail: str) -> None:
        self.raw = raw
        self.fail = fail

    def write(self, block: bytes) -> int:
        if self.fail == 'write':
            raise OSError('disk full')
        return self.raw.write(block)

    def flush(self) -> None:
        if self.fail == 'flush':
            raise OSError('flush failed')
        self.raw.flush()

    def close(self) -> None:
        self.raw.close()
        if self.fail == 'close':
            raise OSError('close failed')


class _FakeClient:
    """Stands in for the boto3 client, recording how each call was asked for.

    ``upload_file`` streams the path in blocks the way the SDK does, but keeps a
    running digest instead of the payload, so a large fake upload never needs a
    large in-memory fixture.
    """

    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.deletes: list[dict] = []
        self.puts = 0
        self.lists = 0
        self.gets: list[dict] = []
        #: Returned by ``get_object``; ``Body``/``ContentLength`` are optional.
        self.response: dict = {}
        self.get_failure: Exception | None = None
        self.failure: Exception | None = None
        self.delete_failure: Exception | None = None
        #: Called with the path after a successful upload, to simulate a race.
        self.on_upload = None
        #: Largest block the fake was asked to read while "uploading".
        self.max_read = 0
        self.batch_deletes = 0

    def upload_file(self, **kwargs) -> None:
        if self.failure is not None:
            raise self.failure
        with Path(kwargs['Filename']).open('rb') as handle:
            while True:
                block = handle.read(READ_CHUNK)
                if not block:
                    break
                self.max_read = max(self.max_read, len(block))
        self.uploads.append(kwargs)
        if self.on_upload is not None:
            self.on_upload(Path(kwargs['Filename']))

    def delete_object(self, **kwargs) -> None:
        # The attempt is recorded before it can fail, so a test can still see
        # exactly which key was targeted when the delete itself failed.
        self.deletes.append(kwargs)
        if self.delete_failure is not None:
            raise self.delete_failure

    def get_object(self, **kwargs) -> dict:
        self.gets.append(kwargs)
        if self.get_failure is not None:
            raise self.get_failure
        return dict(self.response)

    def put_object(self, **_kwargs) -> None:
        self.puts += 1
        raise AssertionError('documents must not be uploaded as a whole body')

    def delete_objects(self, **_kwargs) -> None:
        self.batch_deletes += 1
        raise AssertionError('documents must not be deleted in batches')

    def list_objects_v2(self, **_kwargs) -> None:
        self.lists += 1
        raise AssertionError('deletion must not list the bucket')


class StoreDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / 'upload.pdf'
        self.payload = b'%PDF-1.7\n' + b'body' * 4096
        self.path.write_bytes(self.payload)
        self.client: _FakeClient | None = _FakeClient()
        #: When set, the factory raises it instead of returning the bundle.
        self.factory_error: Exception | None = None
        # The factory reads self.client and self.factory_error on every call, so a
        # test that swaps in a new fake or an error is actually exercising it.
        built = patch.object(store_module, 'get_s3_client', side_effect=self._factory)
        built.start()
        self.addCleanup(built.stop)

    def _factory(self) -> tuple[S3Config, _FakeClient] | None:
        if self.factory_error is not None:
            raise self.factory_error
        if self.client is None:
            return None
        return _CONFIG, self.client

    def _store(self, **kwargs) -> object:
        options = {
            'path': self.path,
            'knowledge_base_id': BASE_ID,
            'document_id': DOC_ID,
            'upload_token': TOKEN,
            'mime_type': 'application/pdf',
        }
        options.update(kwargs)
        return store_document(**options)

    def test_a_valid_pdf_is_hashed_and_uploaded_with_real_values(self) -> None:
        stored = self._store()

        self.assertEqual(
            f'knowledge/{BASE_ID}/{DOC_ID}/{TOKEN}.pdf',
            stored.key,
        )
        self.assertEqual(len(self.payload), stored.size_bytes)
        self.assertEqual(hashlib.sha256(self.payload).hexdigest(), stored.sha256)
        self.assertEqual(1, len(self.client.uploads))
        upload = self.client.uploads[0]
        self.assertEqual(str(self.path), upload['Filename'])
        self.assertEqual('knowledge', upload['Bucket'])
        self.assertEqual(stored.key, upload['Key'])
        self.assertEqual({'ContentType': 'application/pdf'}, upload['ExtraArgs'])
        self.assertEqual(0, self.client.puts)
        self.assertEqual(0, self.client.lists)

    def test_a_text_file_is_accepted_with_its_own_extension(self) -> None:
        plain = Path(self._dir.name) / 'notes.txt'
        plain.write_bytes(b'hello knowledge\n')

        stored = self._store(path=plain, mime_type='text/plain')

        self.assertTrue(stored.key.endswith(f'/{TOKEN}.txt'))
        self.assertEqual({'ContentType': 'text/plain'}, self.client.uploads[0]['ExtraArgs'])

    def test_each_attempt_token_gets_its_own_key(self) -> None:
        first = self._store()
        second = self._store(upload_token=OTHER_TOKEN)

        self.assertNotEqual(first.key, second.key)
        self.assertEqual(f'knowledge/{BASE_ID}/{DOC_ID}/{OTHER_TOKEN}.pdf', second.key)
        self.assertEqual([first.key, second.key], [call['Key'] for call in self.client.uploads])

    def test_the_file_name_and_unicode_path_never_reach_the_key(self) -> None:
        odd = Path(self._dir.name) / 'd\u00f6c ument-\u79c1\u5bc6.pdf'
        odd.write_bytes(self.payload)

        stored = self._store(path=odd)

        self.assertEqual(f'knowledge/{BASE_ID}/{DOC_ID}/{TOKEN}.pdf', stored.key)
        self.assertNotIn('d\u00f6c', stored.key)
        self.assertNotIn(str(self._dir.name), stored.key)

    def test_the_upload_is_streamed_in_bounded_blocks(self) -> None:
        large = Path(self._dir.name) / 'big.txt'
        large.write_bytes(b'z' * (3 * READ_CHUNK + 11))

        with patch.object(Path, 'read_bytes', side_effect=AssertionError('read into memory')):
            stored = self._store(path=large, mime_type='text/plain')

        self.assertEqual(3 * READ_CHUNK + 11, stored.size_bytes)
        self.assertLessEqual(self.client.max_read, READ_CHUNK)

    def test_the_upload_concurrency_is_one_and_classic(self) -> None:
        self._store()

        config = self.client.uploads[0]['Config']
        self.assertIsInstance(config, TransferConfig)
        self.assertEqual(1, config.max_concurrency)
        self.assertEqual('classic', config.preferred_transfer_client)

    def test_a_sparse_file_over_the_limit_is_refused_before_any_upload(self) -> None:
        huge = Path(self._dir.name) / 'huge.txt'
        with huge.open('wb') as handle:
            handle.truncate(MAX_FILE_BYTES + 1)

        with self.assertRaises(DocumentStoreError) as ctx:
            self._store(path=huge, mime_type='text/plain')

        self.assertEqual('document.too_large', ctx.exception.code)
        self.assertEqual([], self.client.uploads)

    def test_an_empty_file_is_refused(self) -> None:
        empty = Path(self._dir.name) / 'empty.txt'
        empty.write_bytes(b'')

        with self.assertRaises(DocumentStoreError) as ctx:
            self._store(path=empty, mime_type='text/plain')

        self.assertEqual('document.empty', ctx.exception.code)
        self.assertEqual([], self.client.uploads)

    def test_a_directory_is_refused(self) -> None:
        with self.assertRaises(DocumentStoreError) as ctx:
            self._store(path=Path(self._dir.name))

        self.assertEqual('document.unreadable', ctx.exception.code)
        self.assertEqual([], self.client.uploads)

    def test_a_missing_file_is_refused(self) -> None:
        with self.assertRaises(DocumentStoreError) as ctx:
            self._store(path=self.path.with_name('gone.pdf'))

        self.assertEqual('document.unreadable', ctx.exception.code)
        self.assertEqual([], self.client.uploads)

    def test_an_unsupported_mime_type_is_refused_without_uploading(self) -> None:
        for mime in ('image/png', 'application/pdf; charset=utf-8', 'APPLICATION/PDF', '', None):
            with self.subTest(mime=mime), self.assertRaises(DocumentStoreError) as ctx:
                self._store(mime_type=mime)

            self.assertEqual('document.unsupported', ctx.exception.code)
        self.assertEqual([], self.client.uploads)

    def test_a_non_canonical_identifier_is_refused_without_uploading(self) -> None:
        bad_ids = (
            ('knowledge_base_id', 'not-a-uuid'),
            ('document_id', str(uuid.uuid4()).upper()),
            ('upload_token', '{33333333-3333-4333-8333-333333333333}'),
            ('upload_token', None),
        )
        for field, value in bad_ids:
            with self.subTest(field=field, value=value), self.assertRaises(DocumentStoreError) as ctx:
                self._store(**{field: value})

            self.assertEqual('document.invalid_input', ctx.exception.code)
        self.assertEqual([], self.client.uploads)

    def test_a_missing_object_store_is_an_error(self) -> None:
        self.client = None

        with self.assertRaises(DocumentStoreError) as ctx:
            self._store()

        self.assertEqual('document.store_unconfigured', ctx.exception.code)

    def test_a_client_build_failure_is_wrapped_without_leaking_its_text(self) -> None:
        self.factory_error = RuntimeError('private-endpoint/token')

        with self.assertRaises(DocumentStoreError) as ctx:
            self._store()

        error = ctx.exception
        self.assertEqual('document.store_unavailable', error.code)
        self.assertNotIn('private-endpoint', str(error))
        self.assertNotIn('token', str(error))
        self.assertIsInstance(error.__cause__, RuntimeError)
        # The fake was never handed out, so no upload or cleanup could happen.
        self.assertEqual([], self.client.uploads)
        self.assertEqual([], self.client.deletes)

    def test_an_upload_failure_cleans_up_only_this_attempt_key(self) -> None:
        self.client.failure = RuntimeError('S3UploadFailedError: access denied to s3://secret')

        with self.assertRaises(DocumentStoreError) as ctx:
            self._store()

        error = ctx.exception
        self.assertEqual('document.upload_failed', error.code)
        self.assertFalse(error.cleanup_failed)
        # The SDK text and any bucket/path never reach the caller-facing message.
        self.assertNotIn('access denied', str(error))
        self.assertNotIn('s3://', str(error))
        self.assertEqual(
            [{'Bucket': 'knowledge', 'Key': f'knowledge/{BASE_ID}/{DOC_ID}/{TOKEN}.pdf'}],
            self.client.deletes,
        )
        self.assertEqual([], self.client.uploads)

    def test_a_failed_upload_reports_when_its_own_cleanup_failed(self) -> None:
        self.client.failure = RuntimeError('upload failed')
        self.client.delete_failure = RuntimeError('delete failed')

        with self.assertRaises(DocumentStoreError) as ctx:
            self._store()

        self.assertEqual('document.upload_failed', ctx.exception.code)
        self.assertTrue(ctx.exception.cleanup_failed)
        # The original upload failure is what surfaces; cleanup is internal info.
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_a_file_changed_during_upload_is_an_error_and_cleans_up(self) -> None:
        self.client.on_upload = lambda target: target.write_bytes(b'')

        with self.assertRaises(DocumentStoreError) as ctx:
            self._store()

        self.assertEqual('document.changed', ctx.exception.code)
        self.assertEqual(f'knowledge/{BASE_ID}/{DOC_ID}/{TOKEN}.pdf', self.client.deletes[0]['Key'])
        # Only one attempt existed, so nothing else could have been removed.
        self.assertEqual(1, len(self.client.deletes))

    def test_a_file_replaced_during_upload_is_an_error(self) -> None:
        def _replace(target: Path) -> None:
            swapped = target.with_name('swapped.pdf')
            swapped.write_bytes(self.payload)
            swapped.replace(target)

        self.client.on_upload = _replace

        with self.assertRaises(DocumentStoreError) as ctx:
            self._store()

        self.assertEqual('document.changed', ctx.exception.code)
        self.assertEqual(1, len(self.client.deletes))

    def test_a_previous_winner_object_is_never_deleted_by_this_attempt(self) -> None:
        winner = self._store(upload_token=OTHER_TOKEN)
        self.client.delete_failure = RuntimeError('delete failed')
        self.client.failure = RuntimeError('upload failed')

        with self.assertRaises(DocumentStoreError):
            self._store()

        self.assertEqual(f'knowledge/{BASE_ID}/{DOC_ID}/{TOKEN}.pdf', self.client.deletes[0]['Key'])
        self.assertNotEqual(winner.key, self.client.deletes[0]['Key'])


class DeleteDocumentObjectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client: _FakeClient | None = _FakeClient()
        #: When set, the factory raises it instead of returning the bundle.
        self.factory_error: Exception | None = None
        built = patch.object(store_module, 'get_s3_client', side_effect=self._factory)
        built.start()
        self.addCleanup(built.stop)
        self.key = f'knowledge/{BASE_ID}/{DOC_ID}/{TOKEN}.pdf'

    def _factory(self) -> tuple[S3Config, _FakeClient] | None:
        if self.factory_error is not None:
            raise self.factory_error
        if self.client is None:
            return None
        return _CONFIG, self.client

    def test_exactly_one_key_is_deleted(self) -> None:
        self.assertIsNone(delete_document_object(self.key))

        self.assertEqual([{'Bucket': 'knowledge', 'Key': self.key}], self.client.deletes)
        self.assertEqual(0, self.client.lists)
        self.assertEqual(0, self.client.puts)

    def test_a_missing_object_store_is_an_error(self) -> None:
        self.client = None

        with self.assertRaises(DocumentStoreError) as ctx:
            delete_document_object(self.key)

        self.assertEqual('document.store_unconfigured', ctx.exception.code)

    def test_a_client_build_failure_is_wrapped_without_leaking_its_text(self) -> None:
        self.factory_error = RuntimeError('private-endpoint/token')

        with self.assertRaises(DocumentStoreError) as ctx:
            delete_document_object(self.key)

        error = ctx.exception
        self.assertEqual('document.store_unavailable', error.code)
        self.assertNotIn('private-endpoint', str(error))
        self.assertNotIn('token', str(error))
        self.assertIsInstance(error.__cause__, RuntimeError)
        self.assertEqual([], self.client.deletes)

    def test_a_key_outside_the_knowledge_prefix_is_refused(self) -> None:
        for key in ('mp/image/1.jpg', 'media/audio/1.mp3', '', None, 42):
            with self.subTest(key=key), self.assertRaises(DocumentStoreError) as ctx:
                delete_document_object(key)

            self.assertEqual('document.invalid_input', ctx.exception.code)
        self.assertEqual([], self.client.deletes)

    def test_a_delete_failure_is_an_error_not_a_success(self) -> None:
        self.client.delete_failure = RuntimeError('AccessDenied')

        with self.assertRaises(DocumentStoreError) as ctx:
            delete_document_object(self.key)

        self.assertEqual('document.delete_failed', ctx.exception.code)
        self.assertNotIn('AccessDenied', str(ctx.exception))


class BuildDocumentKeyTest(unittest.TestCase):
    """The pure key builder the upload state machine persists before any S3 call."""

    def test_the_key_needs_no_object_store_or_local_io(self) -> None:
        # Any client lookup at all is a failure: the key is built before storage.
        with patch.object(store_module, 'get_s3_client', side_effect=AssertionError('no SDK')):
            key = build_document_key(
                knowledge_base_id=BASE_ID,
                document_id=DOC_ID,
                upload_token=TOKEN,
                mime_type='application/pdf',
            )

        self.assertEqual(f'knowledge/{BASE_ID}/{DOC_ID}/{TOKEN}.pdf', key)

    def test_the_builder_matches_the_uploaded_key(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'upload.txt'
            path.write_bytes(b'hello\n')
            client = _FakeClient()
            with patch.object(store_module, 'get_s3_client', return_value=(_CONFIG, client)):
                stored = store_document(
                    path=path,
                    knowledge_base_id=BASE_ID,
                    document_id=DOC_ID,
                    upload_token=TOKEN,
                    mime_type='text/plain',
                )

        expected = build_document_key(
            knowledge_base_id=BASE_ID,
            document_id=DOC_ID,
            upload_token=TOKEN,
            mime_type='text/plain',
        )
        self.assertEqual(expected, stored.key)
        self.assertEqual(expected, client.uploads[0]['Key'])

    def test_invalid_input_is_refused_without_any_sdk_call(self) -> None:
        bad = (
            {'mime_type': 'image/png'},
            {'mime_type': None},
            {'knowledge_base_id': 'not-a-uuid'},
            {'document_id': 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'},
            {'upload_token': '{33333333-3333-4333-8333-333333333333}'},
        )
        with patch.object(store_module, 'get_s3_client', side_effect=AssertionError('no SDK')):
            for overrides in bad:
                options = {
                    'knowledge_base_id': BASE_ID,
                    'document_id': DOC_ID,
                    'upload_token': TOKEN,
                    'mime_type': 'application/pdf',
                }
                options.update(overrides)
                with self.subTest(overrides=overrides), self.assertRaises(DocumentStoreError):
                    build_document_key(**options)


class DownloadDocumentTest(unittest.TestCase):
    """Pulling one verified object back into a fresh private file.

    The SDK is faked, the payloads are tiny and the fake body answers each
    ``read`` from a prepared list, so short/long/altered streams and close
    failures are all exercised without a real service or a large buffer.
    """

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / 'download.pdf'
        self.payload = b'%PDF-1.7\n' + b'body' * 1024
        self.blocks = [self.payload]
        self.body = _FakeBody([])
        self.client: _FakeClient | None = _FakeClient()
        self.factory_error: Exception | None = None
        built = patch.object(store_module, 'get_s3_client', side_effect=self._factory)
        built.start()
        self.addCleanup(built.stop)
        self.client.response = {'Body': self.body, 'ContentLength': len(self.payload)}

    def _factory(self) -> tuple[S3Config, _FakeClient] | None:
        if self.factory_error is not None:
            raise self.factory_error
        if self.client is None:
            return None
        return _CONFIG, self.client

    def _body(self, blocks: list[bytes], **kwargs) -> _FakeBody:
        """Install a fresh fake body that answers the prepared blocks."""
        self.body = _FakeBody(blocks, **kwargs)
        self.client.response = {'Body': self.body, 'ContentLength': len(self.payload)}
        return self.body

    def _download(self, **kwargs) -> None:
        options = {
            'key': DOWNLOAD_KEY,
            'path': self.path,
            'size_bytes': len(self.payload),
            'sha256': hashlib.sha256(self.payload).hexdigest(),
        }
        options.update(kwargs)
        return download_document(**options)

    def test_a_known_object_is_streamed_into_the_fresh_file(self) -> None:
        self._body([self.payload[:5], self.payload[5:], b''])

        self.assertIsNone(self._download())

        self.assertEqual(self.payload, self.path.read_bytes())
        self.assertEqual([{'Bucket': 'knowledge', 'Key': DOWNLOAD_KEY}], self.client.gets)
        # Two data blocks plus the final empty read that marks EOF.
        self.assertEqual(3, self.body.reads)
        self.assertEqual(1, self.body.closed)
        self.assertEqual(0, self.client.puts)
        self.assertEqual(0, self.client.lists)
        self.assertEqual(0, self.client.batch_deletes)
        self.assertEqual([], self.client.deletes)

    def test_a_missing_content_length_still_succeeds_on_the_real_bytes(self) -> None:
        self._body([self.payload])
        self.client.response = {'Body': self.body}

        self._download()

        self.assertEqual(self.payload, self.path.read_bytes())

    def test_a_mismatched_content_length_is_refused_before_writing(self) -> None:
        self._body([self.payload])
        self.client.response = {'Body': self.body, 'ContentLength': len(self.payload) + 1}

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.content_mismatch', ctx.exception.code)
        self.assertEqual(0, self.body.reads)
        self.assertEqual(1, self.body.closed)
        self.assertFalse(self.path.exists())

    def test_a_short_stream_is_refused_although_it_hashed_cleanly(self) -> None:
        short = self.payload[: len(self.payload) - 1]
        self._body([short])

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.content_mismatch', ctx.exception.code)
        self.assertFalse(self.path.exists())

    def test_a_long_stream_fails_before_the_excess_is_written(self) -> None:
        self._body([self.payload, b'extra'])

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.content_mismatch', ctx.exception.code)
        self.assertFalse(self.path.exists())

    def test_an_altered_stream_with_the_right_length_is_refused(self) -> None:
        altered = b'x' * len(self.payload)
        self._body([altered])

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.content_mismatch', ctx.exception.code)
        self.assertFalse(self.path.exists())

    def test_every_read_is_explicitly_bounded(self) -> None:
        self._body([self.payload[:100], self.payload[100:]])

        with patch.object(Path, 'read_bytes', side_effect=AssertionError('read into memory')):
            self._download()

        self.assertGreater(len(self.body.requests), 0)
        for size in self.body.requests:
            self.assertGreater(size, 0)
            self.assertLessEqual(size, READ_CHUNK)

    def test_a_get_failure_is_wrapped_without_leaking_its_text(self) -> None:
        self.client.get_failure = RuntimeError('AccessDenied for knowledge/secret/path')

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        error = ctx.exception
        self.assertEqual('document.download_failed', error.code)
        self.assertNotIn('AccessDenied', str(error))
        self.assertNotIn('knowledge/', str(error))
        self.assertIsInstance(error.__cause__, RuntimeError)
        self.assertFalse(self.path.exists())

    def test_a_missing_body_is_an_error(self) -> None:
        self.client.response = {'ContentLength': len(self.payload)}

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.download_failed', ctx.exception.code)
        self.assertFalse(self.path.exists())

    def test_a_read_failure_closes_the_body_and_cleans_up(self) -> None:
        self._body([b'partial'], read_error=RuntimeError('connection reset'))

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.download_failed', ctx.exception.code)
        self.assertNotIn('connection reset', str(ctx.exception))
        self.assertEqual(1, self.body.closed)
        self.assertFalse(self.path.exists())

    def test_a_body_close_failure_is_not_a_success(self) -> None:
        self._body([self.payload], close_error=RuntimeError('close failed'))

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.download_failed', ctx.exception.code)
        self.assertGreaterEqual(self.body.closed, 1)
        self.assertFalse(self.path.exists())

    def test_a_file_write_flush_or_close_failure_is_a_failure_and_cleans_up(self) -> None:
        # The target is really created and then only fails at the file layer, so
        # a local write/close problem can never be reported as a success.
        for fail in ('write', 'flush', 'close'):
            with self.subTest(fail=fail):
                self.path.unlink(missing_ok=True)
                self._body([self.payload])
                real_open = Path.open

                def _open(target_self, *args, real_open=real_open, fail=fail, **kwargs) -> object:
                    return _FailingHandle(real_open(target_self, *args, **kwargs), fail=fail)

                with (
                    patch.object(Path, 'open', new=_open),
                    self.assertRaises(DocumentStoreError) as ctx,
                ):
                    self._download()

                self.assertEqual('document.download_failed', ctx.exception.code)
                self.assertNotIn('disk full', str(ctx.exception))
                self.assertFalse(self.path.exists())
                self.assertEqual([], self.client.deletes)

    def test_a_client_build_failure_is_wrapped_and_creates_nothing(self) -> None:
        self.factory_error = RuntimeError('private-endpoint/token')

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        error = ctx.exception
        self.assertEqual('document.store_unavailable', error.code)
        self.assertNotIn('private-endpoint', str(error))
        self.assertIsInstance(error.__cause__, RuntimeError)
        self.assertFalse(self.path.exists())

    def test_a_missing_object_store_is_an_error(self) -> None:
        self.client = None

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.store_unconfigured', ctx.exception.code)
        self.assertFalse(self.path.exists())

    def test_an_existing_target_is_kept_untouched(self) -> None:
        self.path.write_bytes(b"somebody else's file")

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.invalid_input', ctx.exception.code)
        self.assertEqual(b"somebody else's file", self.path.read_bytes())
        self.assertEqual([], self.client.gets)
        self.assertEqual([], self.client.deletes)

    def test_a_symlink_target_is_refused_and_not_removed(self) -> None:
        victim = Path(self._dir.name) / 'victim.bin'
        victim.write_bytes(b'untouched')
        self.path.symlink_to(victim)

        with self.assertRaises(DocumentStoreError) as ctx:
            self._download()

        self.assertEqual('document.invalid_input', ctx.exception.code)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(b'untouched', victim.read_bytes())

    def test_invalid_arguments_are_refused_before_any_sdk_call(self) -> None:
        digest = hashlib.sha256(self.payload).hexdigest()
        bad = (
            {'size_bytes': 0},
            {'size_bytes': True},
            {'size_bytes': '12'},
            {'size_bytes': MAX_FILE_BYTES + 1},
            {'sha256': digest.upper()},
            {'sha256': digest[:63]},
            {'sha256': 'z' * 64},
            {'sha256': None},
            {'key': 'mp/image/1.jpg'},
            {'key': ''},
            {'key': None},
        )
        with patch.object(store_module, 'get_s3_client', side_effect=AssertionError('no SDK')):
            for overrides in bad:
                with self.subTest(overrides=overrides), self.assertRaises(DocumentStoreError) as ctx:
                    self._download(**overrides)

                self.assertEqual('document.invalid_input', ctx.exception.code)
        self.assertFalse(self.path.exists())

    def test_an_expired_budget_fails_without_fetching_or_writing(self) -> None:
        self._body([self.payload])
        client = self.client

        with (
            patch.object(store_module.time, 'monotonic', return_value=1000.0),
            patch.object(store_module, '_DOWNLOAD_BUDGET_SECONDS', -1.0),
            self.assertRaises(DocumentStoreError) as ctx,
        ):
            self._download()

        self.assertEqual('document.download_timeout', ctx.exception.code)
        self.assertEqual([], client.gets)
        self.assertEqual([], client.deletes)
        self.assertFalse(self.path.exists())

    def test_a_budget_crossed_only_at_the_end_is_a_timeout(self) -> None:
        # Every read and write happens inside the budget; the body close then
        # moves the clock past the deadline, so the finished file must not be
        # reported as a success.
        clock = [1000.0]
        body = self._body([self.payload])
        real_close = body.close

        def _late_close() -> None:
            clock[0] += store_module._DOWNLOAD_BUDGET_SECONDS
            real_close()

        with (
            patch.object(store_module.time, 'monotonic', side_effect=lambda: clock[0]),
            patch.object(body, 'close', new=_late_close),
            self.assertRaises(DocumentStoreError) as ctx,
        ):
            self._download()

        self.assertEqual('document.download_timeout', ctx.exception.code)
        self.assertEqual(1, self.body.closed)
        self.assertEqual([], self.client.deletes)
        self.assertEqual(0, self.client.puts)
        self.assertEqual(0, self.client.lists)
        self.assertEqual(0, self.client.batch_deletes)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
