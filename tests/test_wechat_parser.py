from __future__ import annotations

import unittest
from pathlib import Path

from hippo.downloader import _parse_markdown_blocks
from hippo.wechat_parser import extract_cgi_data, parse_wechat_article

_SAMPLES_ROOT = Path('/home/sab/wechat-article-exporter/samples')
_ARTICLE_URL = 'https://mp.weixin.qq.com/s/test?__biz=fake&mid=1&idx=1'


class WechatParserSamplesTest(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
