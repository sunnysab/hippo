"""Images have to survive the HTML -> Markdown -> blocks -> download chain.

WeChat's client-protocol HTML ships images with ``data-src`` and no ``src``.
Anything that reads only ``src`` loses every image in the body, and a Markdown
image with an empty URL leaves its *title* behind as the only URL-shaped value —
which once became a request to ``https://mp.weixin.qq.com/s/"1.png"`` and stored
an HTML error page as the image.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from markdownify import markdownify

from hippo.downloader import _image_url, parse_markdown_blocks
from hippo.normalize_html import promote_lazy_image_srcs
from hippo.wechat_parser import _postprocess_markdown


class PromoteLazyImageSrcsTest(unittest.TestCase):
    def test_a_lazy_source_becomes_the_source(self) -> None:
        html = '<p><img data-src="https://mmbiz.qpic.cn/a.jpg" alt="图" /></p>'

        promoted = promote_lazy_image_srcs(html)

        self.assertIn('src="https://mmbiz.qpic.cn/a.jpg"', promoted)

    def test_an_existing_source_is_never_overwritten(self) -> None:
        html = '<img src="https://example.test/real.jpg" data-src="https://example.test/other.jpg" />'

        self.assertEqual(html, promote_lazy_image_srcs(html))

    def test_an_image_without_any_source_is_left_alone(self) -> None:
        html = '<img alt="占位" />'

        self.assertEqual(html, promote_lazy_image_srcs(html))

    def test_html_without_images_is_returned_unchanged(self) -> None:
        html = '<p>没有图片</p>'

        self.assertEqual(html, promote_lazy_image_srcs(html))


class MarkdownImageUrlTest(unittest.TestCase):
    def test_the_markdown_title_is_not_the_url(self) -> None:
        alt, url = '图片', _image_url('https://mmbiz.qpic.cn/a.jpg')
        self.assertEqual('https://mmbiz.qpic.cn/a.jpg', url)
        self.assertEqual('图片', alt)

    def test_a_title_shaped_value_is_refused(self) -> None:
        for value in ('"1.png"', '1.png', '', '  ', 'data:image/gif;base64,R0lGOD'):
            self.assertIsNone(_image_url(value), value)

    def test_a_quoted_absolute_url_is_refused(self) -> None:
        # A quote can only come from the Markdown title, never from a real URL.
        self.assertIsNone(_image_url('https://mmbiz.qpic.cn/a.jpg "1.png"'))


class ParseMarkdownBlocksTest(unittest.TestCase):
    # An image before any title or text is the cover; these tests are about the
    # body, so they start the document the way a synced article does.
    HEAD = '# 标题\n\n正文\n\n'

    def _image_blocks(self, markdown: str) -> list[dict]:
        _title, _cover, blocks, _body = parse_markdown_blocks(self.HEAD + markdown)
        return [block for block in blocks if block.get('type') == 'image']

    def test_a_titled_image_keeps_only_the_url(self) -> None:
        blocks = self._image_blocks('![图片](https://mmbiz.qpic.cn/a.jpg "1.png")\n')

        self.assertEqual(1, len(blocks))
        self.assertEqual('https://mmbiz.qpic.cn/a.jpg', blocks[0]['local_path'])
        self.assertEqual('图片', blocks[0]['alt'])

    def test_an_image_with_no_url_yields_no_download_target(self) -> None:
        # `![]( "1.png")` is what a `data-src`-only image used to convert to.
        blocks = self._image_blocks('![]( "1.png")\n')

        self.assertEqual(1, len(blocks))
        self.assertIsNone(blocks[0].get('local_path'))

    def test_the_real_chain_keeps_every_image(self) -> None:
        html = (
            '<section><p>正文</p>'
            '<img data-src="https://mmbiz.qpic.cn/a.jpg" alt="图" title="1.png" />'
            '<img data-src="https://mmbiz.qpic.cn/b.jpg" alt="图二" />'
            '</section>'
        )

        markdown = _postprocess_markdown(markdownify(promote_lazy_image_srcs(html), heading_style='ATX'))
        blocks = self._image_blocks(markdown)

        self.assertEqual(
            ['https://mmbiz.qpic.cn/a.jpg', 'https://mmbiz.qpic.cn/b.jpg'],
            [block['local_path'] for block in blocks],
        )

    def test_the_first_image_of_an_untitled_body_is_the_cover(self) -> None:
        html = '<img data-src="https://mmbiz.qpic.cn/cover.jpg" alt="封面" />'

        markdown = markdownify(promote_lazy_image_srcs(html), heading_style='ATX')
        _title, cover, blocks, _body = parse_markdown_blocks(markdown)

        self.assertEqual('https://mmbiz.qpic.cn/cover.jpg', cover)
        self.assertEqual([], [block for block in blocks if block.get('type') == 'image'])


if __name__ == '__main__':
    unittest.main()


class OriginFallbackTest(unittest.IsolatedAsyncioTestCase):
    """An image we do not have yet is fetched from WeChat on demand.

    The fallback used to call `asyncio.run` from inside the running event loop,
    so it never fetched anything and the reader got a 502.
    """

    async def test_an_unstored_image_is_fetched_and_returned(self) -> None:
        import hippo.image_hashes as image_hashes

        storage = SimpleNamespace()
        row = {
            'content_type': None,
            's3_key': None,
            'orig_url': 'https://mmbiz.qpic.cn/a.jpg',
            'referer': 'https://mp.weixin.qq.com/s/x',
        }

        with (
            patch.object(image_hashes, 'fetchone_row', AsyncMock(return_value=row)),
            patch.object(image_hashes, 'get_s3_client', lambda: None),
            patch.object(
                image_hashes,
                '_download_image_from_origin',
                AsyncMock(return_value=(b'image-bytes', 'image/jpeg')),
            ) as download,
            patch.object(image_hashes, '_store_image_to_s3_async') as store,
        ):
            payload, content_type = await image_hashes.fetch_image_bytes(storage, 7)

        self.assertEqual(b'image-bytes', payload)
        self.assertEqual('image/jpeg', content_type)
        download.assert_awaited_once()
        store.assert_called_once()
