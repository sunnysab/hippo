"""Tests for the standalone PDF/TXT parse component.

Real Poppler extraction runs against a hand-built, xref-correct PDF generated
here; no PDF library is imported and no fixture file is shipped. The remaining
cases synthesise files with the standard library and stub the tool runner, so
resource/error handling is exercised even where the native tools are absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from hippo.knowledge import parse
from hippo.knowledge.parse import DocumentParseError, parse_document


def _pdf_literal(text: str) -> str:
    escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    return f'({escaped})'


def _build_pdf(page_texts: list[str]) -> bytes:
    """Assemble a minimal, xref-correct PDF with one Helvetica text per page.

    Empty strings produce pages with an empty content stream, which is how the
    empty-page and duplicate-page cases are modelled.
    """
    page_count = len(page_texts)
    page_objects = [4 + 2 * index for index in range(page_count)]
    content_objects = [5 + 2 * index for index in range(page_count)]

    objects: dict[int, bytes] = {
        1: b'<< /Type /Catalog /Pages 2 0 R >>',
        2: (
            '<< /Type /Pages /Kids [{}] /Count {} >>'.format(
                ' '.join(f'{number} 0 R' for number in page_objects), page_count
            )
        ).encode('ascii'),
        3: b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    }
    for index, text in enumerate(page_texts):
        stream = f'BT /F1 24 Tf 72 720 Td {_pdf_literal(text)} Tj ET'.encode('ascii') if text else b''
        objects[content_objects[index]] = b'<< /Length %d >>\nstream\n%s\nendstream' % (len(stream), stream)
        objects[page_objects[index]] = (
            '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            f'/Resources << /Font << /F1 3 0 R >> >> '
            f'/Contents {content_objects[index]} 0 R >>'
        ).encode('ascii')

    output = bytearray(b'%PDF-1.4\n')
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(output)
        output += f'{number} 0 obj\n'.encode('ascii') + objects[number] + b'\nendobj\n'
    xref_offset = len(output)
    total = max(objects) + 1
    output += f'xref\n0 {total}\n'.encode('ascii')
    output += b'0000000000 65535 f \n'
    for number in range(1, total):
        output += f'{offsets[number]:010d} 00000 n \n'.encode('ascii')
    output += (f'trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n').encode('ascii')
    return bytes(output)


def _fake_runner(responses: list[tuple[int, bytes, bytes]], calls: list[dict]) -> object:
    def run(argv, workdir, env, *, timeout=parse.PROCESS_TIMEOUT_SECONDS):
        calls.append({'argv': argv, 'workdir': workdir, 'env': env, 'timeout': timeout})
        if not responses:
            raise AssertionError('unexpected extra tool invocation')
        return responses.pop(0)

    return run


class _TimeoutProcess:
    pid = 4242

    def __init__(self) -> None:
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits == 1:
            raise subprocess.TimeoutExpired(cmd='pdftotext', timeout=timeout)
        return 0

    def kill(self) -> None:
        pass


class _RecordingStr(str):
    """A ``str`` that records the length of every slice taken from it.

    Used to prove the long-line splitter never copies the whole remainder of a
    segment again on every cut.
    """

    def __new__(cls, value: str) -> _RecordingStr:
        instance = super().__new__(cls, value)
        instance.slices = []
        return instance

    def __getitem__(self, item):
        piece = super().__getitem__(item)
        if isinstance(item, slice):
            self.slices.append(len(piece))
        return piece


class ParseDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def _sparse(self, name: str, size: int) -> Path:
        path = self.dir / name
        with open(path, 'wb') as handle:
            handle.truncate(size)
        return path

    def _require_poppler(self) -> None:
        for name in ('pdfinfo', 'pdftotext', 'prlimit'):
            if shutil.which(name) is None:
                self.skipTest(f'{name} is required for real PDF extraction tests')

    # -- TXT ------------------------------------------------------------

    def test_parses_utf8_text_into_paragraph_chunks(self) -> None:
        content = 'first paragraph\n\nsecond paragraph\n\nthird paragraph'
        path = self._write('plain.txt', content.encode('utf-8'))

        result = parse_document(path, 'text/plain')

        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.encoding, 'utf-8')
        self.assertEqual(result.empty_pages, [])
        self.assertEqual([chunk.page_no for chunk in result.chunks], [1, 1, 1])
        self.assertEqual([chunk.ordinal for chunk in result.chunks], [0, 1, 2])
        self.assertEqual(''.join(chunk.text for chunk in result.chunks), content)
        for chunk in result.chunks:
            self.assertEqual(chunk.chunk_type, 'paragraph')
            self.assertIsNone(chunk.heading_level)

    def test_mime_type_parameters_are_ignored(self) -> None:
        path = self._write('plain.txt', b'hello')
        result = parse_document(path, 'text/plain; charset=utf-8')
        self.assertEqual(result.chunks[0].text, 'hello')

    def test_utf8_bom_is_stripped(self) -> None:
        path = self._write('bom.txt', b'\xef\xbb\xbfhello world')

        result = parse_document(path, 'text/plain')

        self.assertEqual(result.encoding, 'utf-8')
        self.assertEqual(result.chunks[0].text, 'hello world')

    def test_utf16_bom_is_decoded(self) -> None:
        path = self._write('utf16.txt', '你好 world'.encode('utf-16'))

        result = parse_document(path, 'text/plain')

        self.assertEqual(result.encoding, 'utf-16')
        self.assertEqual(result.chunks[0].text, '你好 world')

    def test_utf32_bom_wins_over_utf16_prefix(self) -> None:
        path = self._write('utf32.txt', '你好 мира'.encode('utf-32'))

        result = parse_document(path, 'text/plain')

        self.assertEqual(result.encoding, 'utf-32')
        self.assertEqual(result.chunks[0].text, '你好 мира')

    def test_gb18030_fallback_without_bom(self) -> None:
        # "中" is 0xD6 0xD0 in GB18030, which is not valid UTF-8, so the strict
        # UTF-8 attempt must fail before the GB18030 retry succeeds.
        content = '中文测试'
        path = self._write('gb.txt', content.encode('gb18030'))

        result = parse_document(path, 'text/plain')

        self.assertEqual(result.encoding, 'gb18030')
        self.assertEqual(result.chunks[0].text, content)

    def test_invalid_encoding_is_rejected(self) -> None:
        path = self._write('bad.txt', b'abc\xff\xfe\xff')

        with self.assertRaises(DocumentParseError) as ctx:
            parse_document(path, 'text/plain')

        self.assertEqual(ctx.exception.code, parse.CODE_ENCODING)

    def test_binary_nul_is_rejected(self) -> None:
        path = self._write('binary.txt', b'hello\x00world')

        with self.assertRaises(DocumentParseError) as ctx:
            parse_document(path, 'text/plain')

        self.assertEqual(ctx.exception.code, parse.CODE_ENCODING)

    def test_whitespace_only_text_is_rejected(self) -> None:
        path = self._write('blank.txt', b'   \n\t  \n')

        with self.assertRaises(DocumentParseError) as ctx:
            parse_document(path, 'text/plain')

        self.assertEqual(ctx.exception.code, parse.CODE_NO_TEXT)

    def test_long_text_keeps_every_character_and_bounds_chunks(self) -> None:
        content = ('lorem ipsum paragraph body\n\n' * 3000) + 'UNIQUE_TAIL_MARKER'
        self.assertGreater(len(content), 50000)
        path = self._write('long.txt', content.encode('utf-8'))

        result = parse_document(path, 'text/plain')

        self.assertEqual(''.join(chunk.text for chunk in result.chunks), content)
        self.assertTrue(any('UNIQUE_TAIL_MARKER' in chunk.text for chunk in result.chunks))
        self.assertTrue(all(len(chunk.text) <= parse.MAX_CHUNK_CHARS for chunk in result.chunks))
        self.assertGreater(len(result.chunks), 1)

    def test_single_huge_line_is_split_without_loss(self) -> None:
        content = 'a' * (parse.MAX_CHUNK_CHARS * 2 + 123)
        path = self._write('line.txt', content.encode('utf-8'))

        result = parse_document(path, 'text/plain')

        self.assertEqual(''.join(chunk.text for chunk in result.chunks), content)
        self.assertEqual(
            [len(chunk.text) for chunk in result.chunks],
            [parse.MAX_CHUNK_CHARS, parse.MAX_CHUNK_CHARS, 123],
        )

    def test_duplicate_paragraphs_are_kept(self) -> None:
        content = 'dup\n\ndup\n\ndup'
        path = self._write('dup.txt', content.encode('utf-8'))

        result = parse_document(path, 'text/plain')

        self.assertEqual([chunk.text for chunk in result.chunks], ['dup\n\n', 'dup\n\n', 'dup'])
        self.assertEqual(''.join(chunk.text for chunk in result.chunks), content)

    def test_oversized_file_is_rejected_before_tools_run(self) -> None:
        path = self._sparse('huge.pdf', parse.MAX_FILE_BYTES + 1)
        tool = mock.patch.object(parse, '_run_tool', side_effect=AssertionError('tool ran'))
        with tool, self.assertRaises(DocumentParseError) as ctx:
            parse_document(path, 'application/pdf')

        self.assertEqual(ctx.exception.code, parse.CODE_TOO_LARGE)

    def test_oversized_text_is_rejected(self) -> None:
        path = self._write('toobig.txt', b'a' * (parse.MAX_TEXT_CHARS + 1))

        with self.assertRaises(DocumentParseError) as ctx:
            parse_document(path, 'text/plain')

        self.assertEqual(ctx.exception.code, parse.CODE_TOO_LARGE)

    def test_unsupported_mime_type_is_rejected(self) -> None:
        path = self._write('doc.bin', b'anything')

        with self.assertRaises(DocumentParseError) as ctx:
            parse_document(path, 'application/vnd.oasis.opendocument.text')

        self.assertEqual(ctx.exception.code, parse.CODE_UNSUPPORTED)

    def test_path_error_hides_private_path(self) -> None:
        missing = self.dir / 'missing.pdf'

        with self.assertRaises(DocumentParseError) as ctx:
            parse_document(missing, 'application/pdf')

        self.assertEqual(ctx.exception.code, parse.CODE_NOT_FOUND)
        self.assertNotIn(str(self.dir), str(ctx.exception))

    def test_directory_is_not_a_document(self) -> None:
        with self.assertRaises(DocumentParseError) as ctx:
            parse_document(self.dir, 'text/plain')

        self.assertEqual(ctx.exception.code, parse.CODE_NOT_FOUND)

    # -- PDF plumbing ---------------------------------------------------

    def test_limited_argv_sets_all_fixed_limits(self) -> None:
        argv = parse._limited_argv('/usr/bin/prlimit', '/usr/bin/pdftotext', ['/tmp/x.pdf', '-'])

        self.assertEqual(argv[0], '/usr/bin/prlimit')
        self.assertIn('--as=536870912', argv)
        self.assertIn('--cpu=60', argv)
        self.assertIn('--fsize=8388608', argv)
        self.assertEqual(
            argv[argv.index('--') :],
            ['--', '/usr/bin/pdftotext', '/tmp/x.pdf', '-'],
        )

    def test_missing_pdf_dependency_is_reported(self) -> None:
        path = self._write('doc.pdf', _build_pdf(['Alpha']))
        find_tool = mock.patch.object(parse, '_find_tool', return_value=None)
        with find_tool, self.assertRaises(DocumentParseError) as ctx:
            parse_document(path, 'application/pdf')

        self.assertEqual(ctx.exception.code, parse.CODE_DEPENDENCY_MISSING)

    def test_read_bounded_never_slurps_oversized_file(self) -> None:
        path = self._write('big.bin', b'x' * 32)

        with self.assertRaises(DocumentParseError) as ctx:
            parse._read_bounded(path, 8)

        self.assertEqual(ctx.exception.code, parse.CODE_TOO_LARGE)
        self.assertEqual(parse._read_bounded(path, 32), b'x' * 32)

    def test_timeout_kills_the_process_group(self) -> None:
        path = self._write('doc.pdf', _build_pdf(['Alpha']))
        process = _TimeoutProcess()
        killpg_calls: list[tuple[int, int]] = []
        find_tool = mock.patch.object(parse, '_find_tool', side_effect=lambda name: f'/usr/bin/{name}')
        popen = mock.patch.object(subprocess, 'Popen', return_value=process)
        getpgid = mock.patch.object(parse.os, 'getpgid', return_value=4242)
        killpg = mock.patch.object(
            parse.os,
            'killpg',
            side_effect=lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        raises = self.assertRaises(DocumentParseError)
        with find_tool, popen, getpgid, killpg, raises:
            parse_document(path, 'application/pdf')

        self.assertEqual(raises.exception.code, parse.CODE_TIMEOUT)
        self.assertTrue(killpg_calls)

    def test_tool_that_cannot_start_is_a_resource_error(self) -> None:
        path = self._write('doc.pdf', _build_pdf(['Alpha']))
        find_tool = mock.patch.object(parse, '_find_tool', side_effect=lambda name: f'/usr/bin/{name}')
        popen = mock.patch.object(subprocess, 'Popen', side_effect=OSError('cannot execute'))
        raises = self.assertRaises(DocumentParseError)
        with find_tool, popen, raises:
            parse_document(path, 'application/pdf')

        self.assertEqual(raises.exception.code, parse.CODE_RESOURCE)

    def _parse_with_responses(
        self, pdf: Path, responses: list[tuple[int, bytes, bytes]]
    ) -> tuple[parse.ParsedDocument | None, DocumentParseError | None, list[dict]]:
        calls: list[dict] = []
        runner = _fake_runner(responses, calls)
        find_tool = mock.patch.object(parse, '_find_tool', side_effect=lambda name: f'/usr/bin/{name}')
        run_tool = mock.patch.object(parse, '_run_tool', runner)
        with find_tool, run_tool:
            try:
                return parse_document(pdf, 'application/pdf'), None, calls
            except DocumentParseError as error:
                return None, error, calls

    def test_fake_pdf_run_uses_limits_minimal_env_and_timeout(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, calls = self._parse_with_responses(pdf, [(0, b'Pages: 1\n', b''), (0, b'Alpha\n\f', b'')])

        self.assertIsNone(error)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(calls), 2)
        absolute_pdf = os.path.abspath(str(pdf))
        for call in calls:
            self.assertEqual(call['timeout'], parse.PROCESS_TIMEOUT_SECONDS)
            self.assertEqual(call['argv'][0], '/usr/bin/prlimit')
            self.assertIn(f'--as={parse.MAX_PROCESS_MEMORY_BYTES}', call['argv'])
            self.assertEqual(set(call['env']), {'PATH', 'LC_ALL', 'LANG', 'HOME', 'TMPDIR'})
            self.assertEqual(call['env']['HOME'], str(call['workdir']))
            # Only the caller-provided absolute path is forwarded; no temp file
            # name is built from the uploaded file name.
            self.assertIn(absolute_pdf, call['argv'])
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.chunks[0].text, 'Alpha\n')

    def test_fake_pdf_keeps_empty_pages_in_mixed_document(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(pdf, [(0, b'Pages: 3\n', b''), (0, b'Alpha\n\f\fGamma\n\f', b'')])

        self.assertIsNone(error)
        assert result is not None
        self.assertEqual(result.page_count, 3)
        self.assertEqual(result.empty_pages, [2])
        self.assertEqual([chunk.page_no for chunk in result.chunks], [1, 3])
        self.assertEqual([chunk.ordinal for chunk in result.chunks], [0, 0])

    def test_fake_pdf_keeps_duplicate_pages(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(pdf, [(0, b'Pages: 2\n', b''), (0, b'Same\n\fSame\n\f', b'')])

        self.assertIsNone(error)
        assert result is not None
        self.assertEqual([chunk.page_no for chunk in result.chunks], [1, 2])
        self.assertEqual([chunk.text for chunk in result.chunks], ['Same\n', 'Same\n'])

    def test_fake_pdf_without_text_layer_needs_ocr(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(pdf, [(0, b'Pages: 2\n', b''), (0, b'\f\f', b'')])

        self.assertIsNone(result)
        assert error is not None
        self.assertEqual(error.code, parse.CODE_NEEDS_OCR)

    def test_fake_pdf_encrypted_reports_unsupported_encryption(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(pdf, [(0, b'Pages: 1\nEncrypted: yes\n', b'')])

        self.assertIsNone(result)
        assert error is not None
        self.assertEqual(error.code, parse.CODE_ENCRYPTED)

    def test_fake_pdf_without_pages_is_corrupt(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(pdf, [(0, b'Pages: 0\n', b'')])

        self.assertIsNone(result)
        assert error is not None
        self.assertEqual(error.code, parse.CODE_CORRUPT)

    def test_fake_pdf_page_limit_is_enforced(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(pdf, [(0, f'Pages: {parse.MAX_PDF_PAGES + 1}\n'.encode(), b'')])

        self.assertIsNone(result)
        assert error is not None
        self.assertEqual(error.code, parse.CODE_TOO_LARGE)

    def test_fake_pdf_tool_failure_is_classified(self) -> None:
        cases = [
            ((1, b'', b'Syntax Error: damaged xref'), parse.CODE_CORRUPT),
            (
                (1, b'', b'Command Line Error: Incorrect password'),
                parse.CODE_ENCRYPTED,
            ),
            ((1, b'', b'Error: something odd'), parse.CODE_PARSE_FAILED),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                pdf = self._write(f'doc-{expected}.pdf', b'%PDF-1.4\n')
                result, error, _ = self._parse_with_responses(pdf, [response])
                self.assertIsNone(result)
                assert error is not None
                self.assertEqual(error.code, expected)

    def test_temp_directory_is_removed_after_parse(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        created: list[str] = []
        real_factory = tempfile.TemporaryDirectory

        def factory(*args, **kwargs):
            handle = real_factory(*args, **kwargs)
            created.append(handle.name)
            return handle

        calls: list[dict] = []
        runner = _fake_runner([(0, b'Pages: 1\n', b''), (0, b'Alpha\n\f', b'')], calls)
        tempdir = mock.patch.object(tempfile, 'TemporaryDirectory', factory)
        find_tool = mock.patch.object(parse, '_find_tool', side_effect=lambda name: f'/usr/bin/{name}')
        run_tool = mock.patch.object(parse, '_run_tool', runner)
        with tempdir, find_tool, run_tool:
            result = parse_document(pdf, 'application/pdf')

        self.assertEqual(result.chunks[0].text, 'Alpha\n')
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))

    # -- document-wide chunk budget --------------------------------------

    def test_paragraph_segments_is_lazy_and_lossless(self) -> None:
        text = 'a\n\nb\n\nc'

        segments = parse._paragraph_segments(text)

        self.assertIsInstance(segments, Iterator)
        self.assertEqual(next(segments), 'a\n\n')
        self.assertEqual(''.join(segments), 'b\n\nc')

    def test_txt_at_exactly_max_chunks_succeeds(self) -> None:
        content = 'a\n\n' * (parse.MAX_CHUNKS - 1) + 'a'
        path = self._write('exact.txt', content.encode('utf-8'))

        result = parse_document(path, 'text/plain')

        self.assertEqual(len(result.chunks), parse.MAX_CHUNKS)
        self.assertEqual(''.join(chunk.text for chunk in result.chunks), content)

    def test_txt_above_max_chunks_is_rejected(self) -> None:
        content = 'a\n\n' * parse.MAX_CHUNKS + 'a'
        path = self._write('over.txt', content.encode('utf-8'))

        with self.assertRaises(DocumentParseError) as ctx:
            parse_document(path, 'text/plain')

        self.assertEqual(ctx.exception.code, parse.CODE_TOO_LARGE)
        self.assertNotIn(str(self.dir), str(ctx.exception))

    def test_patched_max_chunks_is_read_at_call_time(self) -> None:
        at_limit = self._write('limit.txt', ('a\n\n' * 4 + 'a').encode('utf-8'))
        over_limit = self._write('limit-over.txt', ('a\n\n' * 5 + 'a').encode('utf-8'))

        with mock.patch.object(parse, 'MAX_CHUNKS', 5):
            result = parse_document(at_limit, 'text/plain')
            self.assertEqual(len(result.chunks), 5)
            with self.assertRaises(DocumentParseError) as ctx:
                parse_document(over_limit, 'text/plain')

        self.assertEqual(ctx.exception.code, parse.CODE_TOO_LARGE)

    def test_over_limit_text_builds_no_chunk_and_few_segments(self) -> None:
        text = 'a\n\n' * 600_000
        seen = 0
        real_segments = parse._paragraph_segments

        def counting(source: str) -> Iterator[str]:
            nonlocal seen
            for segment in real_segments(source):
                seen += 1
                yield segment

        built: list[dict] = []
        real_chunk = parse.Chunk

        def factory(**kwargs):
            built.append(kwargs)
            return real_chunk(**kwargs)

        patches = [
            mock.patch.object(parse, '_paragraph_segments', counting),
            mock.patch.object(parse, 'MAX_CHUNKS', 10),
            mock.patch.object(parse, 'Chunk', side_effect=factory),
        ]
        with patches[0], patches[1], patches[2], self.assertRaises(DocumentParseError) as ctx:
            parse_document(self._write('many.txt', text.encode('utf-8')), 'text/plain')

        self.assertEqual(ctx.exception.code, parse.CODE_TOO_LARGE)
        # The budget aborts after ~10 paragraphs instead of materialising 600k.
        self.assertLessEqual(seen, 12)
        self.assertEqual(built, [])

    def test_long_segment_split_never_slices_the_whole_tail(self) -> None:
        segment = _RecordingStr('x' * (parse.MAX_CHUNK_CHARS * 5 + 1))

        pieces = parse._split_long_segment(segment)

        self.assertEqual(''.join(pieces), str(segment))
        self.assertTrue(all(len(piece) <= parse.MAX_CHUNK_CHARS for piece in pieces))
        self.assertTrue(
            all(length <= parse.MAX_CHUNK_CHARS for length in segment.slices),
            segment.slices,
        )

    def test_long_segment_keeps_the_newline_cut_rule(self) -> None:
        segment = 'a' * 100 + '\n' + 'b' * (parse.MAX_CHUNK_CHARS + 10)

        pieces = parse._split_long_segment(segment)

        self.assertEqual(''.join(pieces), segment)
        self.assertEqual([len(piece) for piece in pieces], [101, parse.MAX_CHUNK_CHARS, 10])

    def test_long_paragraph_newline_boundaries_are_lossless(self) -> None:
        content = ('y' * 99 + '\n') * 100
        path = self._write('breaks.txt', content.encode('utf-8'))

        result = parse_document(path, 'text/plain')

        self.assertEqual(''.join(chunk.text for chunk in result.chunks), content)
        self.assertTrue(all(len(chunk.text) <= parse.MAX_CHUNK_CHARS for chunk in result.chunks))
        # Every cut lands just after a newline, so no line is repeated or lost.
        self.assertEqual([len(chunk.text) for chunk in result.chunks], [4000, 4000, 2000])

    # -- PDF page separators ---------------------------------------------

    def test_split_pdf_pages_drops_only_one_terminator(self) -> None:
        # The reported bug: the terminator of the only page was counted as a
        # missing second page instead of a corrupt page count.
        with self.assertRaises(DocumentParseError) as ctx:
            parse._split_pdf_pages('Alpha\n\f', 2)

        self.assertEqual(ctx.exception.code, parse.CODE_CORRUPT)

    def test_split_pdf_pages_without_formfeed_single_page(self) -> None:
        # No form feed at all: the only fragment is real content, never popped.
        self.assertEqual(parse._split_pdf_pages('Alpha', 1), ['Alpha'])
        with self.assertRaises(DocumentParseError) as ctx:
            parse._split_pdf_pages('Alpha', 2)

        self.assertEqual(ctx.exception.code, parse.CODE_CORRUPT)

    def test_fake_pdf_fewer_separators_than_pages_is_corrupt(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(
            pdf,
            [(0, b'Pages: 3\n', b''), (0, b'Alpha\n\fBeta\n', b'')],
        )

        self.assertIsNone(result)
        assert error is not None
        self.assertEqual(error.code, parse.CODE_CORRUPT)

    def test_fake_pdf_extra_separators_are_corrupt_not_merged(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(
            pdf,
            [(0, b'Pages: 1\n', b''), (0, b'Alpha\n\fBeta\n\f', b'')],
        )

        self.assertIsNone(result)
        assert error is not None
        self.assertEqual(error.code, parse.CODE_CORRUPT)

    def test_fake_pdf_keeps_trailing_blank_pages(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(
            pdf,
            [(0, b'Pages: 3\n', b''), (0, b'Alpha\n\f\f\f', b'')],
        )

        self.assertIsNone(error)
        assert result is not None
        # Only the single trailing terminator is dropped; the two real blank
        # pages survive instead of being stripped or padded.
        self.assertEqual(result.page_count, 3)
        self.assertEqual(result.empty_pages, [2, 3])
        self.assertEqual([chunk.page_no for chunk in result.chunks], [1])

    def test_fake_pdf_final_blank_page_is_kept(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        result, error, _ = self._parse_with_responses(
            pdf,
            [(0, b'Pages: 2\n', b''), (0, b'Alpha\n\f\f', b'')],
        )

        self.assertIsNone(error)
        assert result is not None
        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.empty_pages, [2])

    def test_fake_pdf_chunk_budget_is_shared_across_pages(self) -> None:
        pdf = self._write('doc.pdf', b'%PDF-1.4\n')
        # Three pages with two paragraphs each; a per-page budget would accept
        # this with MAX_CHUNKS=5, a document-wide budget may not.
        responses = [(0, b'Pages: 3\n', b''), (0, b'a\n\nb\n\fc\n\nd\n\fe\n\nf\n\f', b'')]

        with mock.patch.object(parse, 'MAX_CHUNKS', 6):
            result, error, _ = self._parse_with_responses(pdf, list(responses))
        self.assertIsNone(error)
        assert result is not None
        self.assertEqual([chunk.page_no for chunk in result.chunks], [1, 1, 2, 2, 3, 3])

        with mock.patch.object(parse, 'MAX_CHUNKS', 5):
            result, error, _ = self._parse_with_responses(pdf, list(responses))
        # An exhausted budget raises instead of returning the pages parsed so far.
        self.assertIsNone(result)
        assert error is not None
        self.assertEqual(error.code, parse.CODE_TOO_LARGE)

    # -- real Poppler ----------------------------------------------------

    def test_real_pdf_extraction_with_empty_page(self) -> None:
        self._require_poppler()
        pdf = self._write('real.pdf', _build_pdf(['Alpha paragraph', '', 'Gamma paragraph']))

        result = parse_document(pdf, 'application/pdf')

        self.assertEqual(result.page_count, 3)
        self.assertEqual(result.empty_pages, [2])
        self.assertIsNone(result.encoding)
        by_page = {chunk.page_no: chunk.text for chunk in result.chunks}
        self.assertEqual(set(by_page), {1, 3})
        self.assertIn('Alpha paragraph', by_page[1])
        self.assertIn('Gamma paragraph', by_page[3])

    def test_real_pdf_keeps_duplicate_pages(self) -> None:
        self._require_poppler()
        pdf = self._write('real-dup.pdf', _build_pdf(['Repeated line', 'Repeated line']))

        result = parse_document(pdf, 'application/pdf')

        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.empty_pages, [])
        self.assertEqual([chunk.page_no for chunk in result.chunks], [1, 2])
        self.assertEqual(result.chunks[0].text, result.chunks[1].text)

    def test_real_pdf_chunk_budget_is_shared_across_pages(self) -> None:
        self._require_poppler()
        pdf = self._write('real-budget.pdf', _build_pdf(['Alpha', 'Beta', 'Gamma']))

        with mock.patch.object(parse, 'MAX_CHUNKS', 3):
            result = parse_document(pdf, 'application/pdf')
        self.assertEqual(len(result.chunks), 3)

        with mock.patch.object(parse, 'MAX_CHUNKS', 2), self.assertRaises(DocumentParseError) as ctx:
            parse_document(pdf, 'application/pdf')
        self.assertEqual(ctx.exception.code, parse.CODE_TOO_LARGE)


if __name__ == '__main__':
    unittest.main()
