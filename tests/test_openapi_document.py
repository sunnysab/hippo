"""The shared OpenAPI document helper.

The contract files read the app's generated document through it, so the one
property that matters is that each caller gets its own copy: a test that mutated
the shared spec would leak into every later one.
"""

from __future__ import annotations

import unittest

from _openapi import document


class OpenApiDocumentHelperTest(unittest.TestCase):
    def test_each_call_hands_out_its_own_copy(self) -> None:
        first = document()
        self.assertTrue(first['paths'])

        first['paths'].clear()

        self.assertTrue(document()['paths'])


if __name__ == '__main__':
    unittest.main()
