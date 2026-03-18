from __future__ import annotations

import unittest

from hippo.repositories import _content_payload_is_present


class ContentPayloadPresenceTest(unittest.TestCase):
    def test_returns_false_for_empty_payload(self) -> None:
        self.assertFalse(_content_payload_is_present('', '', []))
        self.assertFalse(_content_payload_is_present(None, None, None))
        self.assertFalse(_content_payload_is_present('   ', '   ', '[]'))
        self.assertFalse(_content_payload_is_present('   ', '   ', 'null'))

    def test_returns_true_when_html_exists(self) -> None:
        self.assertTrue(_content_payload_is_present('<p>hello</p>', '', []))

    def test_returns_true_when_markdown_exists(self) -> None:
        self.assertTrue(_content_payload_is_present('', 'hello', []))

    def test_returns_true_when_content_json_has_blocks(self) -> None:
        self.assertTrue(_content_payload_is_present('', '', [{'type': 'paragraph'}]))
        self.assertTrue(_content_payload_is_present('', '', '{"type":"paragraph"}'))


if __name__ == '__main__':
    unittest.main()
