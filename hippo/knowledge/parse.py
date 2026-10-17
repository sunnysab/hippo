"""Standalone PDF/TXT parsing for the private knowledge base.

PDF text is extracted in a separate process: Poppler's ``pdftotext`` and
``pdfinfo`` run through ``prlimit`` with fixed memory, CPU, output-size and
wall-clock limits, a throw-away working directory and a minimal environment.
Nothing derived from the caller's file name reaches an argv element, a path or
a shell, and no PDF script or browser is ever launched.

The module only reads the uploaded file and returns chunk rows ready for
``document_chunk``; it never touches the database, the network or a queue.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# -- fixed internal limits (not user-facing options) ------------------------

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGES = 1000
MAX_TEXT_CHARS = 2_000_000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_PROCESS_MEMORY_BYTES = 512 * 1024 * 1024
PROCESS_CPU_SECONDS = 60
PROCESS_TIMEOUT_SECONDS = 60
MAX_CHUNK_CHARS = 4000
# Whole-document cap shared by every page: a document that needs more chunks is
# rejected instead of being returned partially.
MAX_CHUNKS = 50_000
READ_CHUNK_BYTES = 64 * 1024

# -- stable ASCII error codes ------------------------------------------------

CODE_UNSUPPORTED = 'document.unsupported'
CODE_NOT_FOUND = 'document.not_found'
CODE_TOO_LARGE = 'document.too_large'
CODE_NEEDS_OCR = 'document.needs_ocr'
CODE_ENCRYPTED = 'document.encrypted'
CODE_CORRUPT = 'document.corrupt'
CODE_DEPENDENCY_MISSING = 'document.dependency_missing'
CODE_ENCODING = 'document.encoding'
CODE_NO_TEXT = 'document.no_text'
CODE_TIMEOUT = 'document.timeout'
CODE_RESOURCE = 'document.resource'
CODE_PARSE_FAILED = 'document.parse_failed'


class DocumentParseError(Exception):
    """A parse failure carrying a stable ASCII ``code``.

    ``str(error)`` is a generic, path-free English sentence safe to show to the
    caller; the underlying OS/tool text and any private path stay on the
    ``__cause__`` chain for logs, never in the user-facing message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Chunk:
    """One ``document_chunk`` row. ``ordinal`` restarts at 0 on every page."""

    page_no: int
    ordinal: int
    text: str
    chunk_type: str = 'paragraph'
    heading_level: int | None = None


@dataclass(frozen=True)
class ParsedDocument:
    """Structured parse result.

    ``encoding`` is the detected TXT codec (``None`` for PDF: extraction always
    produces UTF-8). ``empty_pages`` lists 1-based PDF pages that had no text
    layer; those pages intentionally have no chunk.
    """

    page_count: int
    chunks: list[Chunk]
    encoding: str | None = None
    empty_pages: list[int] = field(default_factory=list)


def parse_document(path: Path, mime_type: str) -> ParsedDocument:
    """Parse one PDF or plain-text document into page-ordered chunks.

    Raises :class:`DocumentParseError` with a stable ``code`` for every failure
    mode; partial or truncated text is never returned as a success.
    """
    mime = _normalize_mime(mime_type)
    if mime not in ('application/pdf', 'text/plain'):
        raise DocumentParseError(CODE_UNSUPPORTED, 'This file type is not supported.')
    try:
        source = Path(os.fspath(path))
        info = os.stat(source)
    except (TypeError, ValueError, OSError) as exc:
        raise DocumentParseError(CODE_NOT_FOUND, 'The document could not be read.') from exc
    if not stat.S_ISREG(info.st_mode):
        raise DocumentParseError(CODE_NOT_FOUND, 'The document could not be read.')
    if info.st_size > MAX_FILE_BYTES:
        raise DocumentParseError(CODE_TOO_LARGE, 'The document exceeds the supported size limits.')
    absolute = os.path.abspath(os.fspath(source))
    if mime == 'application/pdf':
        return _parse_pdf(absolute)
    return _parse_txt(absolute)


# -- shared helpers ----------------------------------------------------------


def _normalize_mime(mime_type: Any) -> str | None:
    if not isinstance(mime_type, str):
        return None
    value = mime_type.split(';', 1)[0].strip().lower()
    return value or None


def _find_tool(name: str) -> str | None:
    """Locate a known tool by exact name; no globbing or shell lookup."""
    return shutil.which(name)


def _minimal_env(workdir: Path) -> dict[str, str]:
    """A fresh environment: no tokens, credentials or real home directory."""
    return {
        'PATH': '/usr/bin:/bin',
        'LC_ALL': 'C',
        'LANG': 'C',
        'HOME': str(workdir),
        'TMPDIR': str(workdir),
    }


def _limited_argv(prlimit: str, tool: str, args: list[str]) -> list[str]:
    """Wrap a tool with the fixed resource limits; ``shell=False`` always.

    Byte limits are passed as raw integers so no unit-suffix parsing is needed.
    """
    return [
        prlimit,
        f'--as={MAX_PROCESS_MEMORY_BYTES}',
        f'--cpu={PROCESS_CPU_SECONDS}',
        f'--fsize={MAX_OUTPUT_BYTES}',
        '--',
        tool,
        *args,
    ]


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    """Kill the whole process group a timed-out child was started in."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        with contextlib.suppress(OSError):
            process.kill()


def _read_bounded(path: Path, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes so an oversized file is never slurped."""
    with open(path, 'rb') as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise DocumentParseError(CODE_TOO_LARGE, 'The document exceeds the supported size limits.')
    return data


def _read_prefix(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes; used for diagnostics only, never indexed."""
    with open(path, 'rb') as handle:
        return handle.read(limit)


def _run_tool(
    argv: list[str],
    workdir: Path,
    env: dict[str, str],
    *,
    timeout: float = PROCESS_TIMEOUT_SECONDS,
) -> tuple[int, bytes, bytes]:
    """Run one bounded subprocess, capturing stdout/stderr into finite files.

    Both streams go to files in ``workdir`` (never into memory unbounded), and
    the process runs in its own session so a timeout can kill the whole group.
    """
    stdout_path = workdir / 'stdout.bin'
    stderr_path = workdir / 'stderr.bin'
    try:
        with open(stdout_path, 'wb') as out_file, open(stderr_path, 'wb') as err_file:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
                env=env,
                cwd=str(workdir),
                close_fds=True,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process(process)
                process.wait()
                raise DocumentParseError(CODE_TIMEOUT, 'Document extraction timed out.') from None
        stdout = _read_bounded(stdout_path, MAX_OUTPUT_BYTES)
        stderr = _read_prefix(stderr_path, MAX_STDERR_BYTES)
    except DocumentParseError:
        raise
    except OSError as exc:
        raise DocumentParseError(CODE_RESOURCE, 'Document extraction could not start.') from exc
    return returncode, stdout, stderr


def _classify_tool_failure(returncode: int, stderr: bytes) -> DocumentParseError:
    """Map a non-zero tool exit to a stable public error.

    Stderr text is inspected only to choose a code; it is never shown to
    callers, so tool internals and paths stay out of the response.
    """
    if returncode == -signal.SIGXFSZ:
        return DocumentParseError(CODE_TOO_LARGE, 'The document exceeds the supported size limits.')
    if returncode in (-signal.SIGXCPU, -signal.SIGKILL):
        return DocumentParseError(CODE_RESOURCE, 'Document extraction ran out of resources.')
    text = stderr.decode('utf-8', 'ignore').lower()
    if 'password' in text or 'encrypted' in text:
        return DocumentParseError(CODE_ENCRYPTED, 'Encrypted PDFs are not supported.')
    for needle in (
        'syntax error',
        'damaged',
        'corrupt',
        'not a pdf',
        'invalid pdf',
        "couldn't read",
        'may not be a pdf',
    ):
        if needle in text:
            return DocumentParseError(CODE_CORRUPT, 'The PDF could not be read.')
    return DocumentParseError(CODE_PARSE_FAILED, 'The document could not be parsed.')


# -- TXT ---------------------------------------------------------------------


def _canonical_encoding(name: str) -> str:
    return {'utf-8-sig': 'utf-8'}.get(name, name)


def _bom_encoding(head: bytes) -> str | None:
    # UTF-32 BOMs start with the UTF-16 ones, so they must be checked first.
    if head.startswith(codecs.BOM_UTF32_LE) or head.startswith(codecs.BOM_UTF32_BE):
        return 'utf-32'
    if head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
        return 'utf-16'
    if head.startswith(codecs.BOM_UTF8):
        return 'utf-8-sig'
    return None


def _decode_streaming(path: str, encoding: str) -> str:
    """Strict incremental decode; never uses ``errors='replace'``."""
    decoder = codecs.getincrementaldecoder(encoding)('strict')
    pieces: list[str] = []
    total = 0
    with open(path, 'rb') as handle:
        while True:
            raw = handle.read(READ_CHUNK_BYTES)
            if not raw:
                break
            text = decoder.decode(raw, final=False)
            if '\x00' in text:
                raise DocumentParseError(
                    CODE_ENCODING,
                    'The file is not valid text in a supported encoding.',
                )
            total += len(text)
            if total > MAX_TEXT_CHARS:
                raise DocumentParseError(CODE_TOO_LARGE, 'The document exceeds the supported size limits.')
            pieces.append(text)
    pieces.append(decoder.decode(b'', final=True))
    if '\x00' in pieces[-1]:
        raise DocumentParseError(CODE_ENCODING, 'The file is not valid text in a supported encoding.')
    if total + len(pieces[-1]) > MAX_TEXT_CHARS:
        raise DocumentParseError(CODE_TOO_LARGE, 'The document exceeds the supported size limits.')
    return ''.join(pieces)


def _decode_text(path: str) -> tuple[str, str]:
    """Detect the encoding and decode the whole text without losing characters.

    A BOM picks UTF-16/UTF-32/UTF-8 exclusively. Without a BOM the file is
    validated as strict UTF-8 first and re-opened as GB18030 only on a decode
    error, so no full-file buffer is kept between attempts.
    """
    try:
        with open(path, 'rb') as handle:
            head = handle.read(4)
        bom = _bom_encoding(head)
        if bom is not None:
            try:
                return _decode_streaming(path, bom), _canonical_encoding(bom)
            except UnicodeDecodeError as exc:
                raise DocumentParseError(
                    CODE_ENCODING,
                    'The file is not valid text in a supported encoding.',
                ) from exc
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                return _decode_streaming(path, encoding), _canonical_encoding(encoding)
            except UnicodeDecodeError:
                continue
    except OSError as exc:
        raise DocumentParseError(CODE_RESOURCE, 'The document could not be read.') from exc
    raise DocumentParseError(CODE_ENCODING, 'The file is not valid text in a supported encoding.')


def _parse_txt(path: str) -> ParsedDocument:
    text, encoding = _decode_text(path)
    if not text.strip():
        raise DocumentParseError(CODE_NO_TEXT, 'The file contains no indexable text.')
    chunks = _page_chunks(text, 1, MAX_CHUNKS)
    return ParsedDocument(page_count=1, chunks=chunks, encoding=encoding, empty_pages=[])


# -- chunking ----------------------------------------------------------------


def _paragraph_segments(text: str) -> Iterator[str]:
    """Yield the text cut after every blank-line separator.

    Yielding instead of building a list keeps a pathological document from
    materialising one string per separator; the concatenation of all yielded
    pieces is always ``text``.
    """
    start = 0
    while True:
        index = text.find('\n\n', start)
        if index == -1:
            if start < len(text):
                yield text[start:]
            return
        yield text[start : index + 2]
        start = index + 2


def _split_long_segment(segment: str) -> list[str]:
    """Split one paragraph into pieces of at most ``MAX_CHUNK_CHARS`` chars."""
    if len(segment) <= MAX_CHUNK_CHARS:
        return [segment]
    pieces: list[str] = []
    start = 0
    length = len(segment)
    while length - start > MAX_CHUNK_CHARS:
        # Bounded window and cursor: slicing off the whole tail on every cut
        # copied the remainder of a long line again and again.
        window = segment[start : start + MAX_CHUNK_CHARS]
        cut = window.rfind('\n')
        cut = cut + 1 if cut > 0 else MAX_CHUNK_CHARS
        pieces.append(segment[start : start + cut])
        start += cut
    pieces.append(segment[start:])
    return pieces


def _merge_segments(text: str, limit: int) -> list[str]:
    """Group paragraph segments so that ``''.join(result) == text``.

    A non-whitespace segment flushes the buffer, so whitespace-only separators
    are folded into a neighbouring chunk and no blank chunk is ever emitted.
    Every entry yields at least one chunk, so ``limit`` — the chunks the
    document may still add — bounds this list before it can exceed the budget.
    """
    merged: list[str] = []
    buffer = ''
    for segment in _paragraph_segments(text):
        buffer += segment
        if segment.strip():
            if len(merged) >= limit:
                raise DocumentParseError(
                    CODE_TOO_LARGE,
                    'The document exceeds the supported size limits.',
                )
            merged.append(buffer)
            buffer = ''
    if buffer:
        # Callers reject all-whitespace text, so a leftover buffer here is just
        # trailing separators; appending it keeps the text lossless.
        merged[-1] += buffer
    return merged


def _page_chunks(text: str, page_no: int, remaining: int) -> list[Chunk]:
    """Chunk one page's text; the join of all chunk texts equals ``text``.

    ``remaining`` is how many chunks the whole document may still add. It is
    checked before every append, so an exhausted budget raises instead of
    returning the chunks built so far.
    """
    if not text.strip():
        return []
    chunks: list[Chunk] = []
    ordinal = 0
    for segment in _merge_segments(text, remaining):
        for piece in _split_long_segment(segment):
            if len(chunks) >= remaining:
                raise DocumentParseError(
                    CODE_TOO_LARGE,
                    'The document exceeds the supported size limits.',
                )
            chunks.append(Chunk(page_no=page_no, ordinal=ordinal, text=piece))
            ordinal += 1
    return chunks


# -- PDF ---------------------------------------------------------------------


def _parse_pdf_info(stdout: bytes) -> tuple[int, bool]:
    """Read ``Pages:``/``Encrypted:`` from ``pdfinfo`` output (not user-facing)."""
    text = stdout.decode('latin-1')
    page_count = 0
    encrypted = False
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith('pages:'):
            match = re.search(r'(\d+)', stripped)
            if match:
                page_count = int(match.group(1))
        elif lowered.startswith('encrypted:'):
            encrypted = 'yes' in lowered
    return page_count, encrypted


def _split_pdf_pages(text: str, page_count: int) -> list[str]:
    """Split pdftotext output on its form-feed page separators.

    pdftotext terminates every page, the last included, with one form feed, so
    one trailing blank fragment — and only one — is that terminator rather than
    a page; the split needs at least two fragments for it to have come from a
    form feed, so a single-page text without one is never emptied. After that
    the remaining page count must match ``pdfinfo`` exactly: pages are neither
    padded nor merged, so any mismatch is reported as corruption instead of a
    fabricated page number, while genuinely empty pages are kept as empty text.
    """
    pages = text.split('\f')
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    if len(pages) != page_count:
        raise DocumentParseError(CODE_CORRUPT, 'The PDF could not be read.')
    return pages


def _parse_pdf(path: str) -> ParsedDocument:
    pdfinfo = _find_tool('pdfinfo')
    pdftotext = _find_tool('pdftotext')
    prlimit = _find_tool('prlimit')
    if pdfinfo is None or pdftotext is None or prlimit is None:
        raise DocumentParseError(CODE_DEPENDENCY_MISSING, 'PDF text extraction tools are not available.')

    with tempfile.TemporaryDirectory(prefix='hippo-pdf-') as directory:
        workdir = Path(directory)
        env = _minimal_env(workdir)
        info_argv = _limited_argv(prlimit, pdfinfo, [path])
        returncode, stdout, stderr = _run_tool(info_argv, workdir, env, timeout=PROCESS_TIMEOUT_SECONDS)
        if returncode != 0:
            raise _classify_tool_failure(returncode, stderr)
        page_count, encrypted = _parse_pdf_info(stdout)
        if encrypted:
            raise DocumentParseError(CODE_ENCRYPTED, 'Encrypted PDFs are not supported.')
        if page_count < 1:
            raise DocumentParseError(CODE_CORRUPT, 'The PDF could not be read.')
        if page_count > MAX_PDF_PAGES:
            raise DocumentParseError(CODE_TOO_LARGE, 'The document exceeds the supported size limits.')
        text_argv = _limited_argv(prlimit, pdftotext, ['-enc', 'UTF-8', '-eol', 'unix', path, '-'])
        returncode, stdout, stderr = _run_tool(text_argv, workdir, env, timeout=PROCESS_TIMEOUT_SECONDS)
        if returncode != 0:
            raise _classify_tool_failure(returncode, stderr)

    try:
        text = stdout.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise DocumentParseError(CODE_PARSE_FAILED, 'The document could not be parsed.') from exc
    if len(text) > MAX_TEXT_CHARS:
        raise DocumentParseError(CODE_TOO_LARGE, 'The document exceeds the supported size limits.')

    chunks: list[Chunk] = []
    empty_pages: list[int] = []
    remaining = MAX_CHUNKS
    for page_no, page_text in enumerate(_split_pdf_pages(text, page_count), start=1):
        page_chunks = _page_chunks(page_text, page_no, remaining)
        remaining -= len(page_chunks)
        if page_chunks:
            chunks.extend(page_chunks)
        else:
            empty_pages.append(page_no)
    if not chunks:
        raise DocumentParseError(CODE_NEEDS_OCR, 'The PDF has no extractable text layer; OCR is required.')
    return ParsedDocument(page_count=page_count, chunks=chunks, encoding=None, empty_pages=empty_pages)
