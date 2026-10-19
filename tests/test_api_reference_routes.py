"""API.md's appendix must name exactly the operations the app exposes.

The reference document is the human entry point, so a route added to the app but
not to the appendix (or the reverse) is a documentation bug the reviewer should
not have to find by eye. The set is read from the real ``create_app().openapi()``
and from the appendix table, never from a hard-coded list.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hippo import server

ROOT = Path(__file__).resolve().parent.parent
APPENDIX_MARKER = '## 附录 A'
HTTP_METHODS = ('get', 'post', 'put', 'patch', 'delete')
_ROW = re.compile(r'\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`')


def _spec() -> dict[str, Any]:
    with (
        tempfile.TemporaryDirectory() as static_dir,
        patch('hippo.server.configure_logging'),
        patch('hippo.server.init_telemetry', return_value=None),
        patch('hippo.server.instrument_app'),
    ):
        return server.create_app(static_dir=static_dir, enable_inprocess_sync=False).openapi()


def _exposed() -> set[tuple[str, str]]:
    spec = _spec()
    return {
        (method.upper(), path)
        for path, item in spec['paths'].items()
        for method in item
        if method in HTTP_METHODS
    }


def _documented() -> set[tuple[str, str]]:
    text = (ROOT / 'API.md').read_text(encoding='utf-8')
    appendix = text.split(APPENDIX_MARKER, 1)[1]
    return {(match.group(1), match.group(2)) for match in _ROW.finditer(appendix)}


class ApiReferenceAppendixTest(unittest.TestCase):
    def test_the_appendix_is_not_empty(self) -> None:
        self.assertGreater(len(_documented()), 0, 'appendix table not parsed')

    def test_the_appendix_lists_exactly_the_exposed_operations(self) -> None:
        exposed = _exposed()
        documented = _documented()

        self.assertEqual(
            sorted(exposed - documented),
            [],
            'operations missing from API.md appendix',
        )
        self.assertEqual(
            sorted(documented - exposed),
            [],
            'appendix rows that no longer exist as operations',
        )


if __name__ == '__main__':
    unittest.main()
