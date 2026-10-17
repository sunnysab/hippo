import { fireEvent, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ArticlePayload } from '../../store/articles';
import { ArticleContent } from './ArticleContent';

const payload = (
  blocks: ArticlePayload['content'],
  article: Partial<ArticlePayload['article']> = {},
): ArticlePayload => ({
  article: { id: 1, title: '一期播客', ...article } as ArticlePayload['article'],
  content: blocks,
  content_status: 'ok',
  content_updated_at: null,
  images: [],
});

const renderContent = (
  blocks: ArticlePayload['content'],
  article: Partial<ArticlePayload['article']> = {},
) =>
  render(
    <ArticleContent
      payload={payload(blocks, article)}
      hideSmall={false}
      onImageContextMenu={vi.fn()}
    />,
  );

let playMock: ReturnType<typeof vi.fn>;

describe('ArticleContent media blocks', () => {
  beforeEach(() => {
    // jsdom 没实现媒体播放；这里只关心我们把它当成可 seek 的播放器。
    playMock = vi.fn().mockResolvedValue(undefined);
    window.HTMLMediaElement.prototype.play = playMock as unknown as HTMLMediaElement['play'];
  });

  it('plays an archived video inline, without a poster click and without an iframe', () => {
    const { container } = renderContent([
      {
        type: 'video',
        provider: 'bilibili',
        video_id: 'BV1x',
        url: '/api/media/7/video',
        cover_url: '/api/image/9',
      },
      { type: 'paragraph', text: '开场', t: 0 },
    ]);

    const video = container.querySelector('video')!;
    expect(video.getAttribute('src')).toBe('/api/media/7/video');
    expect(video.getAttribute('preload')).toBe('metadata');
    expect(video.getAttribute('poster')).toBe('/api/image/9');
    expect(video.hasAttribute('playsinline')).toBe(true);
    expect(container.querySelector('iframe')).toBeNull();
    expect(container.querySelector('.reader-video-poster')).toBeNull();
  });

  it('prefers the archived video when a legacy block still carries an embed_url', () => {
    const { container } = renderContent([
      {
        type: 'video',
        provider: 'youtube',
        video_id: 'abc',
        url: '/api/media/3/video',
        embed_url: 'https://www.youtube.com/embed/abc',
      },
    ]);

    expect(container.querySelector('video')!.getAttribute('src')).toBe('/api/media/3/video');
    expect(container.querySelector('iframe')).toBeNull();
  });

  it('never loads a third-party embed and points at the original instead', () => {
    const { container } = renderContent(
      [
        {
          type: 'video',
          provider: 'youtube',
          video_id: 'abc',
          embed_url: 'https://www.youtube.com/embed/abc',
        },
        { type: 'paragraph', text: '正题开始', t: 132 },
      ],
      { link: 'https://www.youtube.com/watch?v=abc' },
    );

    expect(container.querySelector('iframe')).toBeNull();
    expect(container.querySelector('video')).toBeNull();
    expect(container.querySelector('.reader-video-poster')).toBeNull();
    expect(container.textContent).toContain('Video not archived yet');

    // 没留档时锚点只能指向原视频的对应时刻，不能假装能就地 seek。
    const anchor = container.querySelector('a.reader-anchor.is-action')!;
    expect(anchor.getAttribute('href')).toBe('https://www.youtube.com/watch?v=abc&t=132');
  });

  it('does not treat an external url as archived media', () => {
    const { container } = renderContent([
      { type: 'video', provider: 'youtube', url: 'https://www.youtube.com/watch?v=abc' },
    ]);

    expect(container.querySelector('video')).toBeNull();
    expect(container.querySelector('iframe')).toBeNull();
    expect(container.textContent).toContain('Video not archived yet');
  });

  it.each([
    // 反斜杠会被浏览器规范成外站主机，绝不能当同源。
    ['/\\evil.test'],
    ['//evil.test/video'],
    ['/arbitrary'],
    ['/api/media/0/video'],
    ['/api/media/7/audio'],
    ['https://evil.test/api/media/7/video'],
  ])('rejects %s as archived media', (url) => {
    const { container } = renderContent([{ type: 'video', provider: 'youtube', url }]);

    expect(container.querySelector('video')).toBeNull();
    expect(container.querySelector('iframe')).toBeNull();
    expect(container.textContent).toContain('Video not archived yet');
  });

  it.each([
    ['/api/media/7/video'],
    ['/api/media/123456/video'],
  ])('accepts %s as the archived media path', (url) => {
    const { container } = renderContent([{ type: 'video', provider: 'bilibili', url }]);

    expect(container.querySelector('video')!.getAttribute('src')).toBe(url);
    expect(container.querySelector('.reader-video.is-missing')).toBeNull();
  });

  it('seeks and plays the archived video from a transcript anchor', () => {
    const { container } = renderContent([
      { type: 'video', provider: 'bilibili', url: '/api/media/7/video' },
      { type: 'paragraph', text: '正题开始', t: 132 },
    ]);

    const anchor = container.querySelector('.reader-anchor.is-action')!;
    expect(anchor.textContent).toBe('02:12');

    fireEvent.click(anchor);

    expect(container.querySelector('video')!.currentTime).toBe(132);
    expect(playMock).toHaveBeenCalled();
  });

  it('seeks the video when the article has both a video and an audio block', () => {
    const { container } = renderContent([
      { type: 'audio', url: '/api/media/7/audio' },
      { type: 'video', provider: 'bilibili', url: '/api/media/7/video' },
      { type: 'paragraph', text: '正题开始', t: 132 },
    ]);

    fireEvent.click(container.querySelector('.reader-anchor.is-action')!);

    expect(container.querySelector('video')!.currentTime).toBe(132);
    expect(container.querySelector('audio')!.currentTime).toBe(0);
  });

  it('pauses the audio when the video starts', () => {
    const { container } = renderContent([
      { type: 'audio', url: '/api/media/7/audio' },
      { type: 'video', provider: 'bilibili', url: '/api/media/7/video' },
    ]);

    const audio = container.querySelector('audio')!;
    const pauseSpy = vi.fn();
    // jsdom 的 paused 永远是 true，这里只验证「正在播放时会被暂停」这条分支。
    Object.defineProperty(audio, 'paused', { value: false, configurable: true });
    audio.pause = pauseSpy;

    fireEvent.play(container.querySelector('video')!);

    expect(pauseSpy).toHaveBeenCalled();
  });

  it('consumes a rejected autoplay when seeking', async () => {
    const rejection = new Error('NotAllowedError');
    playMock.mockRejectedValue(rejection);
    const onUnhandled = vi.fn();
    window.addEventListener('unhandledrejection', onUnhandled);

    try {
      const { container } = renderContent([
        { type: 'video', provider: 'bilibili', url: '/api/media/7/video' },
        { type: 'paragraph', text: '正题开始', t: 132 },
      ]);

      fireEvent.click(container.querySelector('.reader-anchor.is-action')!);
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(container.querySelector('video')!.currentTime).toBe(132);
      expect(playMock).toHaveBeenCalled();
      expect(onUnhandled).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('unhandledrejection', onUnhandled);
    }
  });

  it('renders the archived audio with range-friendly defaults', () => {
    const { container } = renderContent([{ type: 'audio', url: '/api/media/7/audio', duration: 1830 }]);

    const audio = container.querySelector('audio')!;
    expect(audio.getAttribute('src')).toBe('/api/media/7/audio');
    expect(audio.getAttribute('preload')).toBe('metadata');
  });

  it('turns a transcript anchor into a seek button when there is an audio player', () => {
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
    // 没留档的视频没有可操作的进度轴，做成按钮就是个死控件。
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
