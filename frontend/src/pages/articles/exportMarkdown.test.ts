import { describe, expect, it } from 'vitest';
import { formatDuration } from '../../utils/duration';
import { articleToMarkdown } from './exportMarkdown';
import type { ArticlePayload } from '../../store/articles';

const payload = (content: ArticlePayload['content']): ArticlePayload => ({
  article: {
    id: 1,
    biz: 'biz',
    article_id: 'aid',
    title: '标题',
    item_show_type: 0,
    author: '作者',
    digest: '',
    cover: '',
    link: 'https://mp.weixin.qq.com/s/x',
    source_url: '',
    publish_at: 0,
    created_at: '',
    account_nickname: '',
    account_alias: '',
    account_avatar: '',
    account_avatar_url: '',
    group_id: 1,
    group_name: '',
    image_id: null,
    source: 'weixin',
    read_at: null,
    starred_at: null,
  },
  content,
  content_status: 'ok',
  content_updated_at: null,
  images: [],
});

describe('articleToMarkdown', () => {
  it('keeps heading levels, paragraphs, images and code fences', () => {
    const markdown = articleToMarkdown(payload([
      { type: 'heading', level: 3, text: '小节' },
      { type: 'paragraph', text: '正文 **加粗**' },
      { type: 'image', image_id: 42, alt: '图' },
      { type: 'code', text: '```js\nconst a = 1;\n```' },
    ]));

    expect(markdown).toBe([
      '# 标题',
      '### 小节',
      '正文 **加粗**',
      '![图](/api/image/42)',
      '```js\nconst a = 1;\n```',
    ].join('\n\n'));
  });

  it('produces just the title when content is missing', () => {
    expect(articleToMarkdown(payload(null))).toBe('# 标题');
  });

  it('keeps the transcript anchors and the players in an export', () => {
    const markdown = articleToMarkdown(payload([
      { type: 'audio', url: '/api/media/7/audio' },
      { type: 'paragraph', text: '开场', t: 0 },
      { type: 'paragraph', text: '正题', t: 3723 },
      { type: 'video', provider: 'youtube', video_id: 'abc', embed_url: 'https://www.youtube.com/embed/abc' },
    ]));

    expect(markdown).toBe([
      '# 标题',
      '[音频](/api/media/7/audio)',
      '**[00:00]** 开场',
      '**[1:02:03]** 正题',
      '[视频](https://www.youtube.com/embed/abc)',
    ].join('\n\n'));
  });
});

describe('formatDuration', () => {
  it('formats minutes and seconds, and only adds hours when there are any', () => {
    expect(formatDuration(0)).toBe('00:00');
    expect(formatDuration(62.9)).toBe('01:02');
    expect(formatDuration(3723)).toBe('1:02:03');
  });

  it('has no label when the block has no timestamp', () => {
    expect(formatDuration(undefined)).toBe('');
    expect(formatDuration(Number.NaN)).toBe('');
  });
});
