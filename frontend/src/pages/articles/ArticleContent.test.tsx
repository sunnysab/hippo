import { fireEvent, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ArticlePayload } from '../../store/articles';
import { ArticleContent } from './ArticleContent';

const payload = (blocks: ArticlePayload['content']): ArticlePayload => ({
  article: { id: 1, title: '一期播客' } as ArticlePayload['article'],
  content: blocks,
  content_status: 'ok',
  content_updated_at: null,
  images: [],
});

const renderContent = (blocks: ArticlePayload['content']) =>
  render(<ArticleContent payload={payload(blocks)} hideSmall={false} onImageContextMenu={vi.fn()} />);

describe('ArticleContent media blocks', () => {
  beforeEach(() => {
    // jsdom 没实现媒体播放；这里只关心我们把它当成可 seek 的播放器。
    window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
  });

  it('loads the video player only after the poster is clicked', () => {
    const { container } = renderContent([
      { type: 'video', provider: 'bilibili', video_id: 'BV1x', embed_url: 'https://player/bv1x', cover_url: 'https://cdn/c.jpg' },
      { type: 'paragraph', text: '开场', t: 0 },
    ]);

    expect(container.querySelector('iframe')).toBeNull();
    expect(container.querySelector('.reader-video-poster img')).not.toBeNull();

    fireEvent.click(container.querySelector('.reader-video-poster')!);

    expect(container.querySelector('iframe')?.getAttribute('src')).toBe('https://player/bv1x');
  });

  it('renders the archived audio with range-friendly defaults', () => {
    const { container } = renderContent([{ type: 'audio', url: '/api/media/7/audio', duration: 1830 }]);

    const audio = container.querySelector('audio')!;
    expect(audio.getAttribute('src')).toBe('/api/media/7/audio');
    expect(audio.getAttribute('preload')).toBe('metadata');
  });

  it('turns a transcript anchor into a seek button when there is a player', () => {
    const { container } = renderContent([
      { type: 'audio', url: '/api/media/7/audio' },
      { type: 'paragraph', text: '正题开始', t: 132 },
    ]);

    const anchor = container.querySelector('.reader-anchor.is-action')!;
    expect(anchor.textContent).toBe('02:12');

    fireEvent.click(anchor);

    expect(container.querySelector('audio')!.currentTime).toBe(132);
  });

  it('leaves the anchor as plain text when there is nothing to seek', () => {
    // 视频没有可操作的进度轴，做成按钮就是个死控件。
    const { container } = renderContent([
      { type: 'video', provider: 'youtube', video_id: 'abc', embed_url: 'https://www.youtube.com/embed/abc' },
      { type: 'paragraph', text: '正题开始', t: 132 },
    ]);

    expect(container.querySelector('.reader-anchor.is-action')).toBeNull();
    expect(container.querySelector('span.reader-anchor')?.textContent).toBe('02:12');
  });

  it('does not add an anchor to a paragraph without one', () => {
    const { container } = renderContent([{ type: 'paragraph', text: '普通段落' }]);

    expect(container.querySelector('.reader-anchor')).toBeNull();
  });
});
