import { act, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';
import type { ArticlePayload } from '../../store/articles';
import { ArticleContent } from './ArticleContent';

/**
 * The "watch later" playback contract between the reader and whatever owns the
 * queue: the reader reports a real end, reports that it cannot play, and starts the
 * entry it was asked to start — once. The queue itself lives outside this
 * component, so nothing here advances on its own.
 */

const get = vi.fn();
const send = vi.fn();

vi.mock('../../api', async () => ({
  ...await vi.importActual<typeof import('../../api')>('../../api'),
  apiGet: (...args: unknown[]) => get(...args),
  apiSend: (...args: unknown[]) => send(...args),
}));

type Article = Partial<ArticlePayload['article']>;
type ContentProps = Partial<ComponentProps<typeof ArticleContent>>;

const payload = (
  blocks: ArticlePayload['content'],
  article: Article = {},
): ArticlePayload => ({
  article: { id: 1, title: '一期播客', content_kind: 'audio', ...article } as ArticlePayload['article'],
  content: blocks,
  content_status: 'ok',
  content_updated_at: null,
  images: [],
});

const baseProps = {
  hideSmall: false,
  followPlayback: false,
  miniPlayer: true,
  onImageContextMenu: () => {},
};

const renderContent = (
  blocks: ArticlePayload['content'],
  article: Article = {},
  extra: ContentProps = {},
) => render(<ArticleContent {...baseProps} payload={payload(blocks, article)} {...extra} />);

const audioBlock = { type: 'audio' as const, url: '/api/media/7/audio' };
const videoBlock = { type: 'video' as const, provider: 'bilibili', url: '/api/media/7/video' };
const paragraph = { type: 'paragraph' as const, text: '开场', t: 0 };

/** `ended` does not bubble; the element itself is what reports it. */
const fireEnded = (player: HTMLMediaElement) => fireEvent(player, new Event('ended'));

let playMock: ReturnType<typeof vi.fn>;
// jsdom has no media engine: `readyState` is always 0, so a test that wants a player
// able to start says so here instead of relying on an event jsdom cannot produce.
let readyState = 0;
const setReadyState = (value: number) => { readyState = value; };
const originalReadyState = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'readyState');

/** Delivers a rejection that was held back, the way a slow browser answer arrives. */
const settlePendingRejection = async (reject: (error: Error) => void) => {
  await act(async () => {
    reject(new Error('NotAllowedError'));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
};

beforeEach(() => {
  readyState = 0;
  Object.defineProperty(HTMLMediaElement.prototype, 'readyState', {
    configurable: true,
    get: () => readyState,
  });
  playMock = vi.fn().mockResolvedValue(undefined);
  window.HTMLMediaElement.prototype.play = playMock as unknown as HTMLMediaElement['play'];
});

afterEach(() => {
  if (originalReadyState) {
    Object.defineProperty(HTMLMediaElement.prototype, 'readyState', originalReadyState);
  }
});

describe('WatchLaterPlayback: the end of an entry', () => {
  it('reports an ended podcast once, and never for a pause', () => {
    const onMediaEnded = vi.fn();
    const onMediaError = vi.fn();
    const { container } = renderContent([audioBlock, paragraph], {}, { onMediaEnded, onMediaError });
    const audio = container.querySelector('audio')!;
    // 一集播客要先加载出自己的源,它的结束才算数。
    fireEvent.loadedMetadata(audio);

    fireEvent.pause(audio);
    expect(onMediaEnded).not.toHaveBeenCalled();

    fireEnded(audio);
    expect(onMediaEnded).toHaveBeenCalledTimes(1);
    expect(onMediaEnded).toHaveBeenCalledWith(1);

    // 同一个元素把 «结束» 报两遍不能把队列推进两次。
    fireEnded(audio);
    fireEvent.pause(audio);
    expect(onMediaEnded).toHaveBeenCalledTimes(1);
    expect(onMediaError).not.toHaveBeenCalled();
  });

  it('reports an ended video with the payload article id', () => {
    const onMediaEnded = vi.fn();
    const { container } = renderContent([videoBlock, paragraph], { id: 42 }, { onMediaEnded });
    const video = container.querySelector('video')!;
    fireEvent.loadedMetadata(video);

    fireEnded(video);

    expect(onMediaEnded).toHaveBeenCalledTimes(1);
    expect(onMediaEnded).toHaveBeenCalledWith(42);
  });

  it('ignores the end of the player the reader switched away from', () => {
    const onMediaEnded = vi.fn();
    const { container } = renderContent([videoBlock, audioBlock, paragraph], {}, { onMediaEnded });

    const video = container.querySelector('video')!;
    fireEvent.loadedMetadata(video);
    fireEvent.click(container.querySelector('.reader-audio-toggle')!);
    const audio = container.querySelector('audio')!;
    fireEvent.loadedMetadata(audio);

    // 旧元素已经不在屏幕上;它的结束不能移动队列。
    fireEnded(video);
    expect(onMediaEnded).not.toHaveBeenCalled();

    fireEnded(audio);
    expect(onMediaEnded).toHaveBeenCalledTimes(1);
    expect(onMediaEnded).toHaveBeenCalledWith(1);
  });

  it('ignores an end that arrives after the reader left the article', () => {
    const onMediaEnded = vi.fn();
    const view = renderContent([audioBlock], {}, { onMediaEnded });
    const audio = view.container.querySelector('audio')!;
    fireEvent.loadedMetadata(audio);

    view.unmount();
    fireEnded(audio);

    expect(onMediaEnded).not.toHaveBeenCalled();
  });

  it('does not report a new entry for an end that belongs to the previous source', () => {
    const onMediaEnded = vi.fn();
    const { container, rerender } = renderContent([audioBlock], { id: 1 }, { onMediaEnded });
    const audio = container.querySelector('audio')!;
    fireEvent.loadedMetadata(audio);
    fireEnded(audio);
    expect(onMediaEnded).toHaveBeenLastCalledWith(1);

    // 父级在同一个实例里换成下一篇:同一个 <audio> 只是换了 src。
    rerender(
      <ArticleContent {...baseProps} payload={payload([audioBlock], { id: 2 })} onMediaEnded={onMediaEnded} />,
    );
    // 新的一集还没加载出源,这时上一段播放的收尾不能算到它头上。
    fireEnded(container.querySelector('audio')!);
    expect(onMediaEnded).toHaveBeenCalledTimes(1);

    // 新源自己的播放结束才算。
    fireEvent.loadedMetadata(container.querySelector('audio')!);
    fireEnded(container.querySelector('audio')!);
    expect(onMediaEnded).toHaveBeenCalledTimes(2);
    expect(onMediaEnded).toHaveBeenLastCalledWith(2);
  });
});

describe('WatchLaterPlayback: starting the requested entry', () => {
  it('plays nothing until the entry is asked for, and then only once', () => {
    setReadyState(1);
    const view = renderContent([audioBlock, paragraph]);
    expect(playMock).not.toHaveBeenCalled();

    view.rerender(
      <ArticleContent {...baseProps} autoPlay payload={payload([audioBlock, paragraph])} />,
    );
    expect(playMock).toHaveBeenCalledTimes(1);

    // 纠正过的同一份 payload 不是新的一次播放请求。
    view.rerender(
      <ArticleContent
        {...baseProps}
        autoPlay
        payload={payload([audioBlock, { ...paragraph, text: '开场（校正）' }])}
      />,
    );
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it('starts a mounted player once its metadata is there, and only once', () => {
    const { container } = renderContent([audioBlock, paragraph], {}, { autoPlay: true });
    const audio = container.querySelector('audio')!;
    expect(playMock).not.toHaveBeenCalled();

    setReadyState(1);
    fireEvent.loadedMetadata(audio);
    expect(playMock).toHaveBeenCalledTimes(1);

    fireEvent.loadedMetadata(audio);
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it('does not restart an already running article when the reader switches player', () => {
    setReadyState(1);
    const { container } = renderContent([videoBlock, audioBlock, paragraph], {}, { autoPlay: true });
    expect(playMock).toHaveBeenCalledTimes(1);

    fireEvent.click(container.querySelector('.reader-audio-toggle')!);
    const audio = container.querySelector('audio')!;
    fireEvent.loadedMetadata(audio);

    // 交接位置是交接,不是第二次自动播放。
    expect(audio.currentTime).toBe(0);
    expect(playMock).toHaveBeenCalledTimes(1);
  });

  it('reports a refused autoplay instead of throwing it at the page', async () => {
    setReadyState(1);
    playMock.mockRejectedValue(new Error('NotAllowedError'));
    const onMediaError = vi.fn();
    const onMediaEnded = vi.fn();
    const onUnhandled = vi.fn();
    window.addEventListener('unhandledrejection', onUnhandled);

    try {
      // 不加任何 Provider:读者不依赖 toast 上下文,错误由页面自己说给读者。
      renderContent([audioBlock, paragraph], {}, { autoPlay: true, onMediaError, onMediaEnded });

      await waitFor(() => expect(onMediaError).toHaveBeenCalledWith(1));
      await new Promise((resolve) => setTimeout(resolve, 0));

      // 播放被拒绝既不推进队列,也不该冒出一个未处理的 rejection。
      expect(onMediaEnded).not.toHaveBeenCalled();
      expect(onUnhandled).not.toHaveBeenCalled();
      expect(onMediaError).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener('unhandledrejection', onUnhandled);
    }
  });

  it('drops an autoplay refusal that arrives after the reader moved to the next entry', async () => {
    setReadyState(1);
    let rejectFirstPlay: (error: Error) => void = () => {};
    playMock.mockReturnValueOnce(new Promise<void>((resolve, reject) => { rejectFirstPlay = reject; }));
    const onMediaError = vi.fn();
    const onMediaEnded = vi.fn();
    const view = renderContent([audioBlock, paragraph], { id: 1 }, { autoPlay: true, onMediaError });
    expect(playMock).toHaveBeenCalledTimes(1);

    // 父级换到下一篇,新的一集已经开始挑自己的播放。
    view.rerender(
      <ArticleContent
        {...baseProps}
        autoPlay
        payload={payload([audioBlock, paragraph], { id: 2 })}
        onMediaError={onMediaError}
        onMediaEnded={onMediaEnded}
      />,
    );
    expect(onMediaError).not.toHaveBeenCalled();

    // 上一篇那一次 play() 现在才被拒绝:它不能再停下现在这一集。
    await settlePendingRejection(rejectFirstPlay);

    expect(onMediaError).not.toHaveBeenCalled();
    expect(onMediaEnded).not.toHaveBeenCalled();
  });

  it('drops an autoplay refusal that arrives after the reader closed the entry', async () => {
    setReadyState(1);
    let rejectPlay: (error: Error) => void = () => {};
    playMock.mockReturnValueOnce(new Promise<void>((resolve, reject) => { rejectPlay = reject; }));
    const onMediaError = vi.fn();
    const view = renderContent([audioBlock, paragraph], {}, { autoPlay: true, onMediaError });
    expect(playMock).toHaveBeenCalledTimes(1);

    view.unmount();
    await settlePendingRejection(rejectPlay);

    expect(onMediaError).not.toHaveBeenCalled();
  });

  it('reports a media element that cannot play, and never counts it as an end', () => {
    const onMediaError = vi.fn();
    const onMediaEnded = vi.fn();
    const { container } = renderContent([videoBlock, paragraph], {}, { onMediaError, onMediaEnded });

    fireEvent.error(container.querySelector('video')!);

    expect(onMediaError).toHaveBeenCalledTimes(1);
    expect(onMediaError).toHaveBeenCalledWith(1);
    expect(onMediaEnded).not.toHaveBeenCalled();
  });
});

describe('WatchLaterPlayback: entries with nothing to play', () => {
  it.each(['audio', 'video'] as const)('stops on a %s entry without playable media', (kind) => {
    const onMediaError = vi.fn();
    const onMediaEnded = vi.fn();
    renderContent(
      [{ type: 'paragraph', text: '还没归档的音视频', t: 0 }],
      { content_kind: kind },
      { autoPlay: true, onMediaError, onMediaEnded },
    );

    expect(onMediaError).toHaveBeenCalledTimes(1);
    expect(onMediaError).toHaveBeenCalledWith(1);
    expect(onMediaEnded).not.toHaveBeenCalled();
  });

  it('stops on a media entry whose body is still being transcribed', () => {
    const onMediaError = vi.fn();
    render(
      <ArticleContent
        {...baseProps}
        autoPlay
        payload={{ ...payload(null, { content_kind: 'video' }), content_status: 'processing' }}
        onMediaError={onMediaError}
      />,
    );

    expect(onMediaError).toHaveBeenCalledWith(1);
  });

  it('never automatically starts audio embedded in an ordinary article', () => {
    const onMediaError = vi.fn();
    setReadyState(1);
    const { container } = renderContent([audioBlock, paragraph], { content_kind: 'article' }, { autoPlay: true, onMediaError });
    fireEvent.loadedMetadata(container.querySelector('audio')!);
    expect(playMock).not.toHaveBeenCalled();
    expect(onMediaError).not.toHaveBeenCalled();
  });

  it('stays silent for an ordinary text article', () => {
    const onMediaError = vi.fn();
    renderContent(
      [{ type: 'paragraph', text: '一篇普通文章' }],
      { content_kind: 'article' },
      { autoPlay: true, onMediaError },
    );

    expect(onMediaError).not.toHaveBeenCalled();
  });
});
