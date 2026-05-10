from __future__ import annotations

import unittest
from pathlib import Path
import json

from hippo.downloader import _parse_markdown_blocks
from hippo.wechat_parser import extract_cgi_data, parse_wechat_article

_SAMPLES_ROOT = Path('/home/sab/wechat-article-exporter/samples')
_ARTICLE_URL = 'https://mp.weixin.qq.com/s/test?__biz=fake&mid=1&idx=1'


class WechatParserSamplesTest(unittest.TestCase):
    def _build_raw_html(self, cgi_data: dict) -> str:
        payload = json.dumps(cgi_data, ensure_ascii=False)
        return f'''
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body>
          <div id="js_content"></div>
          <script>
            window.cgiDataNew = {payload};
          </script>
        </body>
        </html>
        '''

    def _read_sample(self, relative_path: str) -> str:
        sample_path = _SAMPLES_ROOT / relative_path
        if not sample_path.exists():
            self.skipTest(f'Sample not found: {sample_path}')
        return sample_path.read_text(encoding='utf-8', errors='ignore')

    def test_extracts_picture_share_cgi_data(self) -> None:
        raw_html = self._read_sample('图片分享/01.html')
        cgi_data = extract_cgi_data(raw_html, article_url=_ARTICLE_URL)

        self.assertEqual(cgi_data.get('item_show_type'), 8)
        self.assertTrue(cgi_data.get('picture_page_info_list'))
        self.assertIn('价格战', str(cgi_data.get('content_noencode') or ''))

    def test_parses_picture_share_into_gallery_blocks(self) -> None:
        raw_html = self._read_sample('图片分享/01.html')
        parsed = parse_wechat_article(raw_html, article_url=_ARTICLE_URL)
        title, _cover_local, blocks, _body_markdown = _parse_markdown_blocks(parsed.markdown)
        image_blocks = [block for block in blocks if block.get('type') == 'image']

        self.assertEqual(parsed.item_show_type, 8)
        self.assertEqual(title, parsed.title)
        self.assertGreaterEqual(len(image_blocks), 4)
        self.assertIn('wechat-picture-gallery', parsed.clean_html)

    def test_parses_text_share_into_text_blocks(self) -> None:
        raw_html = self._read_sample('文本分享/01.html')
        parsed = parse_wechat_article(raw_html, article_url=_ARTICLE_URL)
        title, _cover_local, blocks, _body_markdown = _parse_markdown_blocks(parsed.markdown)

        self.assertEqual(parsed.item_show_type, 10)
        self.assertEqual(title, parsed.title)
        self.assertTrue(any(block.get('type') == 'paragraph' for block in blocks))
        self.assertNotIn('![](', parsed.markdown)

    def test_parses_regular_article_with_many_images(self) -> None:
        raw_html = self._read_sample('普通图文/02.html')
        parsed = parse_wechat_article(raw_html, article_url=_ARTICLE_URL)
        title, _cover_local, blocks, _body_markdown = _parse_markdown_blocks(parsed.markdown)
        image_blocks = [block for block in blocks if block.get('type') == 'image']

        self.assertEqual(parsed.item_show_type, 0)
        self.assertEqual(title, parsed.title)
        self.assertGreaterEqual(len(image_blocks), 20)
        self.assertIn('软件界面如何设计', parsed.markdown)

    def test_parses_music_share_from_structured_cgi_data(self) -> None:
        raw_html = self._build_raw_html(
            {
                'item_show_type': 6,
                'title': '',
                'content_noencode': '一首适合深夜工作的歌。',
                'music_page_info': {
                    'song_name': 'Night Shift',
                    'singer': 'Aster',
                    'album_name': 'After Hours',
                    'cover_url': 'https://example.com/music-cover.jpg',
                    'music_url': 'https://example.com/track',
                },
            }
        )
        parsed = parse_wechat_article(raw_html, article_url=_ARTICLE_URL)
        title, _cover_local, blocks, _body_markdown = _parse_markdown_blocks(parsed.markdown)

        self.assertEqual(parsed.item_show_type, 6)
        self.assertEqual(parsed.title, 'Night Shift')
        self.assertEqual(title, 'Night Shift')
        self.assertTrue(any(block.get('type') == 'image' for block in blocks))
        self.assertTrue(any(block.get('type') == 'heading' and block.get('text') == 'Night Shift' for block in blocks))
        self.assertIn('Aster', parsed.markdown)
        self.assertIn('Source: https://example.com/track', parsed.markdown)

    def test_parses_audio_share_from_voice_card_data(self) -> None:
        raw_html = self._build_raw_html(
            {
                'item_show_type': 7,
                'title': '',
                'digest': '一期关于工程设计判断力的播客。',
                'voice_in_appmsg_list_json': json.dumps(
                    {
                        'voice_in_appmsg': [
                            {
                                'voice_name': 'Design Review 042',
                                'nickname': 'Hippo FM',
                                'play_length': 1540,
                                'cover': 'https://example.com/audio-cover.jpg',
                                'url': 'https://example.com/audio',
                                'appmsgalbuminfo': {'title': 'Engineering Notes'},
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            }
        )
        parsed = parse_wechat_article(raw_html, article_url=_ARTICLE_URL)
        title, _cover_local, blocks, _body_markdown = _parse_markdown_blocks(parsed.markdown)

        self.assertEqual(parsed.item_show_type, 7)
        self.assertEqual(parsed.title, 'Design Review 042')
        self.assertEqual(title, 'Design Review 042')
        self.assertTrue(any(block.get('type') == 'image' for block in blocks))
        self.assertIn('Hippo FM', parsed.markdown)
        self.assertIn('25:40', parsed.markdown)
        self.assertIn('Engineering Notes', parsed.markdown)

    def test_parses_audio_briefing_digest_as_structured_body(self) -> None:
        raw_html = self._build_raw_html(
            {
                'item_show_type': 7,
                'title': '5月6日行业快讯',
                'digest': '\n'.join(
                    [
                        '<a data-unique-id="" data-timestamp="0" class="wx_audio_timepoint_tag">00:00</a> IBM在Think 2026上发布了企业级AI新功能',
                        '<a data-unique-id="" data-timestamp="415" class="wx_audio_timepoint_tag">06:55</a> PVM研究为大型视觉语言模型解决了长序列生成难题',
                    ]
                ),
                'audio_info': {
                    'audio_infos': [
                        {
                            'audio_id': 'demo-audio-id',
                            'title': '5月6日行业快讯',
                            'play_length': 2292,
                        }
                    ]
                },
                'media_duration': '38:12',
                'link': 'https://mp.weixin.qq.com/s/demo-audio-briefing',
            }
        )

        parsed = parse_wechat_article(raw_html, article_url=_ARTICLE_URL)
        title, _cover_local, blocks, body_markdown = _parse_markdown_blocks(parsed.markdown)

        self.assertEqual(parsed.item_show_type, 7)
        self.assertEqual(title, '5月6日行业快讯')
        self.assertNotIn('&lt;a', parsed.clean_html)
        self.assertIn('class="wx_audio_timepoint_tag"', parsed.clean_html)
        self.assertIn('00:00', body_markdown)
        self.assertTrue(any(block.get('type') == 'paragraph' and 'IBM在Think 2026' in block.get('text', '') for block in blocks))

    def test_parses_audio_share_metadata_from_audio_info_payload(self) -> None:
        raw_html = self._build_raw_html(
            {
                'item_show_type': 7,
                'title': '5月6日行业快讯',
                'digest': '一档行业音频快讯。',
                'audio_info': {
                    'audio_infos': [
                        {
                            'audio_id': 'demo-audio-id',
                            'title': '5月6日行业快讯',
                            'play_length': 2292,
                        }
                    ]
                },
                'media_duration': '38:12',
                'link': 'https://mp.weixin.qq.com/s/demo-audio-briefing',
            }
        )

        parsed = parse_wechat_article(raw_html, article_url=_ARTICLE_URL)

        self.assertIn('38:12', parsed.clean_html)
        self.assertIn('Source: https://mp.weixin.qq.com/s/demo-audio-briefing', parsed.markdown)

    def test_parses_short_share_with_cover_and_source(self) -> None:
        raw_html = self._build_raw_html(
            {
                'item_show_type': 17,
                'title': '',
                'short_content': '今天把解析链路彻底换成 cgiDataNew 了。\n整体干净很多。',
                'cover_url': 'https://example.com/short-cover.jpg',
                'short_link': 'https://example.com/short-post',
            }
        )
        parsed = parse_wechat_article(raw_html, article_url=_ARTICLE_URL)
        title, _cover_local, blocks, _body_markdown = _parse_markdown_blocks(parsed.markdown)

        self.assertEqual(parsed.item_show_type, 17)
        self.assertEqual(parsed.title, '今天把解析链路彻底换成 cgiDataNew 了。')
        self.assertEqual(title, parsed.title)
        self.assertTrue(any(block.get('type') == 'image' for block in blocks))
        self.assertTrue(any(block.get('type') == 'paragraph' for block in blocks))
        self.assertIn('Source: https://example.com/short-post', parsed.markdown)

    def test_parse_markdown_blocks_collapses_linked_image_split_by_blank_lines(self) -> None:
        markdown = '\n'.join(
            [
                'Paragraph before',
                '',
                '[',
                '',
                '![](https://img.test/x.png)',
                '',
                '](https://mp.weixin.qq.com/s/demo)',
                '',
                'Paragraph after',
            ]
        )

        _title, _cover_local, blocks, body_markdown = _parse_markdown_blocks(markdown)

        self.assertEqual(
            blocks,
            [
                {'type': 'paragraph', 'text': 'Paragraph before'},
                {
                    'type': 'image',
                    'alt': '',
                    'local_path': 'https://img.test/x.png',
                    'href': 'https://mp.weixin.qq.com/s/demo',
                },
                {'type': 'paragraph', 'text': 'Paragraph after'},
            ],
        )
        self.assertIn('[![](https://img.test/x.png)](https://mp.weixin.qq.com/s/demo)', body_markdown)
        self.assertNotIn('\n[\n', body_markdown)
        self.assertNotIn('](https://mp.weixin.qq.com/s/demo)', [block.get('text') for block in blocks if block.get('type') == 'paragraph'])

    def test_parse_markdown_blocks_splits_audio_timestamp_lines(self) -> None:
        markdown = '\n'.join(
            [
                '# 5月6日行业快讯',
                '',
                '[00:00](https://mp.weixin.qq.com/s/demo) IBM在Think 2026上发布了企业级AI新功能',
                '[06:55](https://mp.weixin.qq.com/s/demo) PVM研究为大型视觉语言模型解决了长序列生成难题',
                '[10:15](https://mp.weixin.qq.com/s/demo) Silk指出了公有云存储的危机',
            ]
        )

        title, _cover_local, blocks, body_markdown = _parse_markdown_blocks(markdown)

        self.assertEqual(title, '5月6日行业快讯')
        self.assertEqual(
            blocks,
            [
                {'type': 'paragraph', 'text': '[00:00](https://mp.weixin.qq.com/s/demo) IBM在Think 2026上发布了企业级AI新功能'},
                {'type': 'paragraph', 'text': '[06:55](https://mp.weixin.qq.com/s/demo) PVM研究为大型视觉语言模型解决了长序列生成难题'},
                {'type': 'paragraph', 'text': '[10:15](https://mp.weixin.qq.com/s/demo) Silk指出了公有云存储的危机'},
            ],
        )
        self.assertEqual(
            body_markdown,
            '\n'.join(
                [
                    '[00:00](https://mp.weixin.qq.com/s/demo) IBM在Think 2026上发布了企业级AI新功能',
                    '[06:55](https://mp.weixin.qq.com/s/demo) PVM研究为大型视觉语言模型解决了长序列生成难题',
                    '[10:15](https://mp.weixin.qq.com/s/demo) Silk指出了公有云存储的危机',
                ]
            ),
        )


if __name__ == '__main__':
    unittest.main()
