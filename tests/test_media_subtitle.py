"""Subtitle parsing and export.

Real subtitle files are messy (inline markup, cue ids, a header, settings after the
timestamp), and the export has to reproduce anchors that came from two different
places: a published transcript or our own chunk boundaries.
"""

import unittest
from typing import ClassVar

from hippo.media.subtitle import (
    Cue,
    format_timestamp,
    parse_timed_text,
    timed_paragraphs,
    to_markdown,
    to_srt,
    to_text,
)

VTT = """WEBVTT

00:00:00.000 --> 00:00:04.000
开场白

00:01:02.500 --> 00:01:06.000 line:0 position:20%
<i>第二段</i>正题开始。
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
第一句

2
00:00:04,000 --> 00:00:06,000
第二句
"""


class ParseTest(unittest.TestCase):
    def test_webvtt_cues(self) -> None:
        cues = parse_timed_text(VTT)

        self.assertEqual(2, len(cues))
        self.assertEqual(0.0, cues[0].start_seconds)
        self.assertEqual(62.5, cues[1].start_seconds)

    def test_cue_settings_and_markup_are_dropped(self) -> None:
        cues = parse_timed_text(VTT)

        self.assertEqual('第二段正题开始。', cues[1].text)

    def test_srt_cues(self) -> None:
        cues = parse_timed_text(SRT)

        self.assertEqual([1.0, 4.0], [cue.start_seconds for cue in cues])
        self.assertEqual('第二句', cues[1].text)

    def test_a_header_only_file_has_no_cues(self) -> None:
        self.assertEqual([], parse_timed_text('WEBVTT\n\n'))

    def test_notes_are_not_cues(self) -> None:
        payload = 'WEBVTT\n\nNOTE 这是一条注释\n\n00:00:01.000 --> 00:00:02.000\n真的句子\n'

        self.assertEqual(['真的句子'], [cue.text for cue in parse_timed_text(payload)])

    def test_a_block_without_a_timestamp_is_ignored(self) -> None:
        self.assertEqual([], parse_timed_text('随便一段文字\n\n再来一段\n'))


class TimestampTest(unittest.TestCase):
    def test_an_srt_timestamp(self) -> None:
        self.assertEqual('01:02:03,000', format_timestamp(3723, comma=True))

    def test_a_vtt_timestamp_passes_the_hour_mark(self) -> None:
        self.assertEqual('00:01:02', format_timestamp(62.5))


class TimedParagraphsTest(unittest.TestCase):
    def test_only_paragraphs_with_text_are_kept(self) -> None:
        blocks = [
            {'type': 'video', 'video_id': 'x'},
            {'type': 'paragraph', 'text': '有内容', 't': 3},
            {'type': 'paragraph', 'text': '   '},
            {'type': 'paragraph', 'text': '没有锚点'},
            'not a block',
        ]

        self.assertEqual([(3.0, '有内容'), (0.0, '没有锚点')], timed_paragraphs(blocks))

    def test_junk_instead_of_blocks_is_empty(self) -> None:
        self.assertEqual([], timed_paragraphs(None))
        self.assertEqual([], timed_paragraphs('{"a": 1}'))


class ExportTest(unittest.TestCase):
    _BLOCKS: ClassVar[list] = [{'type': 'paragraph', 'text': '第一句', 't': 0.0}, {'type': 'paragraph', 'text': '第二句', 't': 62.5}]

    def test_markdown_puts_the_anchor_in_bold(self) -> None:
        self.assertEqual('**[00:00]** 第一句\n\n**[01:02]** 第二句', to_markdown(self._BLOCKS))

    def test_markdown_with_a_title(self) -> None:
        self.assertTrue(to_markdown(self._BLOCKS, title='一期播客').startswith('# 一期播客'))

    def test_text_has_no_anchors(self) -> None:
        self.assertEqual('第一句\n\n第二句', to_text(self._BLOCKS))

    def test_srt_cues_are_numbered_and_end_at_the_next_anchor(self) -> None:
        body = to_srt(self._BLOCKS)

        self.assertIn('1\n00:00:00,000 --> 00:01:02,000\n第一句', body)
        self.assertIn('2\n00:01:02,000 --> 00:01:08,000\n第二句', body)

    def test_srt_of_nothing_is_empty(self) -> None:
        self.assertEqual('', to_srt([]))

    def test_cues_export_through_the_same_path(self) -> None:
        blocks = [{'type': 'paragraph', 'text': cue.text, 't': cue.start_seconds} for cue in [Cue(1.0, '一句')]]

        self.assertEqual('一句', to_text(blocks))


if __name__ == '__main__':
    unittest.main()
