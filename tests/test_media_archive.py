"""Archiving, with the object store faked out.

What is worth pinning down: the media file is handed to the SDK as a *path* so it
is streamed from disk instead of being read whole into Python memory, the upload
concurrency stays small and classic, a file that changes mid-upload fails, and
every kind of failure is an error rather than a half-successful return.
"""

import os
import tracemalloc
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from boto3.s3.transfer import TransferConfig

from hippo.media import archive as archive_module
from hippo.media.archive import ArchiveError, fetch_object, store_media
from hippo.s3 import S3Config

_CONFIG = S3Config(
    endpoint='http://minio.test',
    bucket='media',
    access_key='key',
    secret_key='secret',
    region='us-east-1',
    prefix='mp/image/',
)

_SIZE = 64


class _FakeClient:
    """Stands in for the boto3 client and records how the upload was asked for."""

    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.puts = 0
        self.failure: Exception | None = None
        #: Called with the path after a successful upload, to simulate a race.
        self.on_upload = None

    def upload_file(self, **kwargs) -> None:
        if self.failure is not None:
            raise self.failure
        # A real client opens the path itself; stat is enough to prove it was a path.
        Path(kwargs['Filename']).stat()
        self.uploads.append(kwargs)
        if self.on_upload is not None:
            self.on_upload(Path(kwargs['Filename']))

    def put_object(self, **_kwargs) -> None:
        self.puts += 1
        raise AssertionError('media must not be uploaded as a body')


class StoreMediaTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / 'archive-ep1.mp3'
        self.path.write_bytes(b'x' * _SIZE)
        self.client = _FakeClient()
        built = patch.object(archive_module, 'get_s3_client', return_value=(_CONFIG, self.client))
        built.start()
        self.addCleanup(built.stop)

    def test_the_file_path_is_uploaded_instead_of_a_body(self) -> None:
        key, size = store_media(article_pk=7, path=self.path)

        self.assertEqual('media/audio/7.mp3', key)
        self.assertEqual(_SIZE, size)
        self.assertEqual(1, len(self.client.uploads))
        upload = self.client.uploads[0]
        self.assertEqual(str(self.path), upload['Filename'])
        self.assertEqual('media', upload['Bucket'])
        self.assertEqual('media/audio/7.mp3', upload['Key'])
        self.assertEqual({'ContentType': 'audio/mpeg'}, upload['ExtraArgs'])
        self.assertEqual(0, self.client.puts)

    def test_a_video_keeps_its_kind_and_content_type(self) -> None:
        # guess_extension has no entry for video/mp4, so the existing key rule
        # gives `.bin`; the kind and the ContentType are what carry the format.
        key, size = store_media(article_pk=9, path=self.path, kind='video', content_type='video/mp4')

        self.assertEqual('media/video/9.bin', key)
        self.assertEqual(_SIZE, size)
        self.assertEqual({'ContentType': 'video/mp4'}, self.client.uploads[0]['ExtraArgs'])

    def test_the_size_is_the_file_stat_size(self) -> None:
        # Not an empty upload and not a re-read: the returned size is the real file.
        self.path.write_bytes(b'y' * (_SIZE * 3))

        _, size = store_media(article_pk=7, path=self.path)

        self.assertEqual(_SIZE * 3, size)

    def test_the_file_is_never_read_into_memory(self) -> None:
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('read into memory')):
            key, size = store_media(article_pk=7, path=self.path)

        self.assertEqual('media/audio/7.mp3', key)
        self.assertEqual(_SIZE, size)

    def test_the_upload_concurrency_is_capped(self) -> None:
        store_media(article_pk=7, path=self.path)

        config = self.client.uploads[0]['Config']
        self.assertIsInstance(config, TransferConfig)
        # One part at a time; "small" is the point, not a knob to expose.
        self.assertEqual(1, config.max_concurrency)
        # Pinned to the classic client so the cap is not silently dropped if a future
        # SDK release starts preferring AWS CRT.
        self.assertEqual('classic', config.preferred_transfer_client)

    def test_a_file_that_changes_during_upload_is_an_error(self) -> None:
        # Truncated while the SDK was reading it: the stored object is short, so
        # returning the pre-upload size would claim a match that does not exist.
        self.client.on_upload = lambda target: target.write_bytes(b'')

        with self.assertRaises(ArchiveError) as ctx:
            store_media(article_pk=7, path=self.path)

        self.assertIn('changed during upload', str(ctx.exception))

    def test_a_file_replaced_during_upload_is_an_error(self) -> None:
        # Same size, new inode: only the identity check catches this one.
        def _replace(target: Path) -> None:
            swapped = target.with_name('swapped.mp3')
            swapped.write_bytes(b'x' * _SIZE)
            os.replace(swapped, target)

        self.client.on_upload = _replace

        with self.assertRaises(ArchiveError):
            store_media(article_pk=7, path=self.path)

        self.assertEqual(1, len(self.client.uploads))

    def test_a_large_file_is_not_read_into_memory(self) -> None:
        # Sparse 8 MiB file: no large bytes fixture and no large write. Any whole-file
        # copy (read_bytes, Path.open().read(), shutil, ...) allocates near file size.
        big = Path(self._dir.name) / 'archive-big.mp3'
        with big.open('wb') as handle:
            handle.truncate(8 << 20)
        started_here = not tracemalloc.is_tracing()
        if started_here:
            tracemalloc.start()
        else:
            tracemalloc.reset_peak()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            key, size = store_media(article_pk=7, path=big)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            if started_here:
                tracemalloc.stop()

        self.assertEqual('media/audio/7.mp3', key)
        self.assertEqual(8 << 20, size)
        self.assertLess(peak - baseline, 1 << 20)

    def test_a_missing_object_store_is_an_error(self) -> None:
        with (
            patch.object(archive_module, 'get_s3_client', return_value=None),
            self.assertRaises(ArchiveError),
        ):
            store_media(article_pk=7, path=self.path)

        self.assertEqual([], self.client.uploads)

    def test_an_upload_failure_is_an_error_not_a_success(self) -> None:
        self.client.failure = RuntimeError('S3UploadFailedError: access denied')

        with self.assertRaises(RuntimeError):
            store_media(article_pk=7, path=self.path)

        self.assertEqual([], self.client.uploads)

    def test_a_missing_file_is_an_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            store_media(article_pk=7, path=self.path.with_name('gone.mp3'))

        self.assertEqual([], self.client.uploads)


class _FakeBody:
    """The streaming body boto3 hands back, with a close() worth asserting on."""

    def __init__(self, payload: bytes, *, declared: int | None = None) -> None:
        self.payload = payload
        self.declared = len(payload) if declared is None else declared
        self.closed = False
        self.reads = 0

    def iter_chunks(self, size: int):
        for start in range(0, len(self.payload), size):
            self.reads += 1
            yield self.payload[start : start + size]

    def close(self) -> None:
        self.closed = True


class _FakeReader:
    """Stands in for the S3 client's ``get_object``, and records the call."""

    def __init__(
        self,
        payload: bytes,
        *,
        declared: int | None = None,
        failure: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.declared = declared
        self.failure = failure
        self.close_error = close_error
        self.calls: list[dict] = []
        self.body: _FakeBody | None = None

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        self.body = _FakeBody(self.payload, declared=self.declared)
        if self.close_error is not None:
            error = self.close_error

            def close() -> None:
                raise error

            self.body.close = close
        return {'Body': self.body, 'ContentLength': self.body.declared}


class FetchObjectTest(unittest.TestCase):
    """Reading an archived object back, with the size bounded the same way."""

    #: Two blocks of the read size, so "read it in pieces" is provable with a small file.
    _CHUNK = 1 << 16
    _PAYLOAD_SIZE = 2 * (1 << 16) + 7

    def setUp(self) -> None:
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.target = Path(self._dir.name) / 'source.bin'
        self.payload = b'x' * self._PAYLOAD_SIZE
        self.client = _FakeReader(self.payload)
        # The factory reads self.client on every call, so a test that swaps in a new
        # reader is actually exercising the new fixture rather than the setUp one.
        built = patch.object(archive_module, 'get_s3_client', side_effect=lambda: (_CONFIG, self.client))
        built.start()
        self.addCleanup(built.stop)

    def _fetch(self, **kwargs) -> int:
        return fetch_object('media/video/7.bin', self.target, **kwargs)

    def test_the_object_is_read_back_and_its_length_verified(self) -> None:
        written = self._fetch()

        self.assertEqual(self._PAYLOAD_SIZE, written)
        self.assertEqual(self.payload, self.target.read_bytes())
        self.assertEqual('media/video/7.bin', self.client.calls[0]['Key'])
        # Streamed in 64 KiB pieces rather than read whole into memory.
        self.assertGreater(self.client.body.reads, 2)

    def test_the_body_is_closed_even_after_a_failure(self) -> None:
        # 一条没关的流会把连接留在池子里，也会让下一个 job 见到脏状态。
        self.client = _FakeReader(self.payload, declared=self._PAYLOAD_SIZE + 1)

        with self.assertRaises(ArchiveError):
            self._fetch()

        self.assertTrue(self.client.body.closed)

    def test_a_declared_length_over_the_ceiling_is_refused_before_the_body(self) -> None:
        self.client = _FakeReader(self.payload, declared=self._PAYLOAD_SIZE)

        with self.assertRaises(ArchiveError) as ctx:
            self._fetch(max_bytes=self._PAYLOAD_SIZE - 1)

        self.assertIn('ceiling', str(ctx.exception))
        self.assertFalse(self.target.exists())

    def test_a_body_that_overruns_the_ceiling_is_stopped_and_removed(self) -> None:
        # The declared length is never trusted: the running count is the real gate.
        self.client = _FakeReader(self.payload, declared=8)

        with self.assertRaises(ArchiveError) as ctx:
            self._fetch(max_bytes=8)

        self.assertIn('ceiling', str(ctx.exception))
        self.assertFalse(self.target.exists())

    def test_a_short_object_is_an_error_and_leaves_no_partial_file(self) -> None:
        # The length check lives inside the cleanup path: a short read must not leave
        # its partial file for the next job to use as a source.
        self.client = _FakeReader(self.payload[:8], declared=self._PAYLOAD_SIZE)

        with self.assertRaises(ArchiveError) as ctx:
            self._fetch()

        self.assertIn('incomplete', str(ctx.exception))
        self.assertFalse(self.target.exists())
        self.assertTrue(self.client.body.closed)

    def test_a_read_failure_removes_the_partial_file(self) -> None:
        self.client = _FakeReader(self.payload, failure=RuntimeError('connection reset'))

        with self.assertRaises(ArchiveError) as ctx:
            self._fetch()

        self.assertIn('failed to read', str(ctx.exception))
        self.assertFalse(self.target.exists())

    def test_a_body_close_failure_does_not_hide_the_read_failure(self) -> None:
        self.client = _FakeReader(
            self.payload,
            declared=self._PAYLOAD_SIZE + 1,
            close_error=RuntimeError('socket already closed'),
        )

        with self.assertRaises(ArchiveError) as ctx:
            self._fetch()

        self.assertIn('incomplete', str(ctx.exception))
        self.assertFalse(self.target.exists())

    def test_a_missing_object_store_is_an_error(self) -> None:
        with (
            patch.object(archive_module, 'get_s3_client', return_value=None),
            self.assertRaises(ArchiveError),
        ):
            self._fetch()

        self.assertEqual([], self.client.calls)


if __name__ == '__main__':
    unittest.main()
