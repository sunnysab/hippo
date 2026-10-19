import {
  createContext,
  memo,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useI18n } from '../../i18n';
import { renderInlineNodes } from '../../utils/markdown';
import { EmptyState } from '../../components/EmptyState';
import type { ArticlePayload, ArticleContentBlock } from '../../store/articles';
import { buildContentBlockKeys } from './contentKeys';
import { formatDuration as anchorLabel, timestampHref } from '../../utils/duration';
import { headingAnchorId, isOutlineBlock } from './outline';

/**
 * A transcript is navigated by clicking an anchor in front of a paragraph, which
 * has to reach the player — and the player may be several blocks above. The reader
 * holds that little bit of state here instead of threading it through every block.
 */
interface PlayerControls {
  /** False when the article has nothing playable, which is when anchors are plain text. */
  canSeek: boolean;
  seek: (seconds: number) => void;
  /** Stops whichever player is not `active`, so video and audio never overlap. */
  pauseOther: (active: HTMLMediaElement) => void;
  /**
   * The article's page on its platform, for entries whose video was never
   * archived: Bilibili and YouTube both read `?t=<seconds>` there, so an anchor
   * can still point at the right moment of the original.
   */
  originalHref: string | null;
}

// Only render-time values live here; the two media refs travel down as props so
// that no ref is touched while a block renders.
const PlayerContext = createContext<PlayerControls>({
  canSeek: false,
  seek: () => {},
  pauseOther: () => {},
  originalHref: null,
});

/**
 * The archived-video path the backend hands out is exactly `/api/media/<id>/video`.
 * Anything else is not our media: a protocol URL, `//host`, `/arbitrary`, or a
 * backslash the browser rewrites into a remote host (`/\evil.test`).
 */
const ARCHIVED_VIDEO_PATH = /^\/api\/media\/[1-9]\d*\/video$/;

const isArchivedVideoUrl = (url: string | undefined): url is string =>
  typeof url === 'string' && ARCHIVED_VIDEO_PATH.test(url);

interface ArticleContentProps {
  payload: ArticlePayload;
  hideSmall: boolean;
  onImageContextMenu: (detail: { imageId: number; x: number; y: number }) => void;
}

interface ContentBlockProps {
  block: ArticleContentBlock;
  images: ArticlePayload['images'];
  hideSmall: boolean;
  onImageContextMenu: (detail: { imageId: number; x: number; y: number }) => void;
}

/** Only headings need to know where they sit, for the outline anchor. */
interface AnchorableBlockProps extends ContentBlockProps {
  index: number;
  outlineIndex?: number;
}

const ArticleImageBlock = memo(function ArticleImageBlock({
  block,
  images,
  hideSmall,
  onImageContextMenu,
}: ContentBlockProps) {
  const figureRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const figure = figureRef.current;
    if (!figure) return;

    figure.style.display = '';
    if (!hideSmall) return;

    const img = figure.querySelector('img');
    if (!img) return;

    const onLoad = () => {
      const w = img.naturalWidth;
      const h = img.naturalHeight;
      const imageMeta = (images || []).find((item) => item.id === block.image_id);
      const isGif = imageMeta?.content_type && String(imageMeta.content_type).includes('gif');

      if (isGif) {
        figure.style.display = 'none';
        return;
      }

      if (w < 500 && h < 500) {
        const ratio = w / h;
        if (ratio < 1.2) {
          figure.style.display = 'none';
        }
      }
    };

    if (img.complete) {
      onLoad();
      return;
    }

    img.addEventListener('load', onLoad);
    return () => img.removeEventListener('load', onLoad);
  }, [block.image_id, hideSmall, images]);

  if (!block.image_id) return null;

  return (
    <figure ref={figureRef} data-image-id={String(block.image_id)}>
      <img
        src={`/api/image/${block.image_id}`}
        alt={block.alt || ''}
        loading='lazy'
        data-image-id={String(block.image_id)}
        // An image we cannot serve (origin gone, blocked, upstream down) is dropped
        // like the small ones above, so the reader shows no broken-image glyph.
        onError={(event) => {
          const figure = event.currentTarget.closest('figure');
          if (figure) (figure as HTMLElement).style.display = 'none';
        }}
        onContextMenu={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onImageContextMenu({
            imageId: block.image_id!,
            x: event.clientX,
            y: event.clientY,
          });
        }}
      />
    </figure>
  );
});

interface PlayerRefProps {
  audioRef: React.RefObject<HTMLAudioElement | null>;
  videoRef: React.RefObject<HTMLVideoElement | null>;
  /** Reports the player's position so the transcript can follow along. */
  onTimeUpdate: (event: React.SyntheticEvent<HTMLMediaElement>) => void;
}

const ArticleVideoBlock = memo(function ArticleVideoBlock({
  block,
  videoRef,
  onTimeUpdate,
}: {
  block: ArticleContentBlock;
  videoRef: React.RefObject<HTMLVideoElement | null>;
  onTimeUpdate: PlayerRefProps['onTimeUpdate'];
}) {
  const { t } = useI18n();
  const { originalHref, pauseOther } = useContext(PlayerContext);

  // The archived copy streams from Hippo with Range support, so it plays at once
  // and never contacts the provider — even when a legacy block still carries an
  // `embed_url`.
  if (isArchivedVideoUrl(block.url)) {
    return (
      <div className="reader-video" data-provider={block.provider || ''}>
        <video
          ref={videoRef}
          src={block.url}
          controls
          preload='metadata'
          playsInline
          poster={block.cover_url || undefined}
          onPlay={(event) => pauseOther(event.currentTarget)}
          onTimeUpdate={onTimeUpdate}
          data-testid='reader-video-file'
        />
      </div>
    );
  }

  // Nothing was archived, and a third-party embed is never loaded in its place:
  // say what is missing and leave the original a link away.
  return (
    <div className="reader-video is-missing" data-provider={block.provider || ''}>
      <p className="reader-video-missing">
        {t('articles.videoNotArchived', 'Video not archived yet. Retry it from the transcribe action.')}
      </p>
      {originalHref ? (
        <a
          className="reader-video-origin"
          href={originalHref}
          target="_blank"
          rel="noreferrer"
        >
          {t('articles.openOriginalVideo', 'Open original video')}
        </a>
      ) : null}
    </div>
  );
});

const ArticleAudioBlock = memo(function ArticleAudioBlock({
  block,
  audioRef,
  onTimeUpdate,
}: {
  block: ArticleContentBlock;
  audioRef: React.RefObject<HTMLAudioElement | null>;
  onTimeUpdate: PlayerRefProps['onTimeUpdate'];
}) {
  const { pauseOther } = useContext(PlayerContext);
  if (!block.url) return null;
  return (
    <div className="reader-audio">
      {/* No <track>: for a podcast the transcript below the player *is* the
          caption, and the feed does not ship one in a caption format. */}
      <audio
        ref={audioRef}
        controls
        preload='metadata'
        src={block.url}
        onPlay={(event) => pauseOther(event.currentTarget)}
        onTimeUpdate={onTimeUpdate}
      />
    </div>
  );
});

const ContentBlock = memo(function ContentBlock({
  block,
  index,
  images,
  hideSmall,
  onImageContextMenu,
  audioRef,
  videoRef,
  onTimeUpdate,
  isPlayingBlock,
  outlineIndex,
}: AnchorableBlockProps & PlayerRefProps & { isPlayingBlock: boolean }) {
  const { canSeek, seek, originalHref } = useContext(PlayerContext);

  const anchor = { 'data-outline-block': outlineIndex };

  if (['paragraph', 'text', 'link'].includes(block.type)) {
    const label = anchorLabel(block.t);
    const text = renderInlineNodes(block.text || '');
    const className = isPlayingBlock ? 'is-playing' : undefined;
    if (!label) return <p {...anchor} className={className}>{text}</p>;
    if (!canSeek) {
      // Nothing is archived here, so the anchor opens the original at that moment
      // instead of seeking a player that does not exist.
      const href = originalHref ? timestampHref(originalHref, block.t || 0) : null;
      if (!href) {
        return (
          <p {...anchor} className={className}>
            <span className="reader-anchor" title={label}>{label}</span>
            {text}
          </p>
        );
      }
      return (
        <p {...anchor} className={className}>
          <a className="reader-anchor is-action" href={href} target="_blank" rel="noreferrer" title={label}>
            {label}
          </a>
          {text}
        </p>
      );
    }
    return (
      <p {...anchor} className={className}>
        <button
          className="reader-anchor is-action"
          type="button"
          onClick={() => seek(block.t || 0)}
          title={label}
        >
          {label}
        </button>
        {text}
      </p>
    );
  }

  if (block.type === 'video') {
    return (
      <ArticleVideoBlock
        block={block}
        videoRef={videoRef}
        onTimeUpdate={onTimeUpdate}
      />
    );
  }

  if (block.type === 'audio') {
    return (
      <ArticleAudioBlock
        block={block}
        audioRef={audioRef}
        onTimeUpdate={onTimeUpdate}
      />
    );
  }

  if (block.type === 'heading') {
    // The id is what the outline links to; it must match `buildOutline`.
    const id = headingAnchorId(index);
    const level = Math.min(Math.max(Number(block.level) || 2, 2), 4);
    if (level === 2) {
      return <h2 {...anchor} id={id}>{renderInlineNodes(block.text || '')}</h2>;
    }
    if (level === 3) {
      return <h3 {...anchor} id={id}>{renderInlineNodes(block.text || '')}</h3>;
    }
    return <h4 {...anchor} id={id}>{renderInlineNodes(block.text || '')}</h4>;
  }

  if (block.type === 'image') {
    return (
      <ArticleImageBlock
        block={block}
        images={images}
        hideSmall={hideSmall}
        onImageContextMenu={onImageContextMenu}
      />
    );
  }

  if (block.type === 'code') {
    const codeText = (block.text || '')
      .replace(/^```+[^\n]*\n?/, '')
      .replace(/\n?```+$/, '')
      .trimEnd();
    return (
      <pre {...anchor} className="code-block"><code>{codeText}</code></pre>
    );
  }

  return null;
});

export function ArticleContent({ payload, hideSmall, onImageContextMenu }: ArticleContentProps) {
  const { t } = useI18n();

  const content = payload.content;
  const status = String(payload.content_status || '').trim().toLowerCase();

  if (!Array.isArray(content)) {
    return (
      <div className="reader">
        <EmptyState message={
          status === 'processing'
            ? t('articles.contentProcessing', '正在转写这份音视频，稍后回来看。')
            : status === 'invalid'
              ? t('articles.contentInvalid', 'Failed to parse article content. Please try syncing again.')
              : t('articles.contentMissing', 'Article content is not available yet. Please wait for sync to finish or sync again.')
        } />
      </div>
    );
  }

  if (content.length === 0) {
    return (
      <div className="reader">
        <EmptyState message={t('articles.contentEmpty', 'This article has no content.')} />
      </div>
    );
  }

  return (
    <RenderedContent
      payload={payload}
      content={content}
      hideSmall={hideSmall}
      onImageContextMenu={onImageContextMenu}
    />
  );
}

interface RenderedContentProps extends ArticleContentProps {
  content: ArticleContentBlock[];
}

/**
 * Renders the blocks and owns the one piece of cross-block state: the players the
 * transcript anchors seek. An archived video wins over a podcast audio track when
 * both exist, and the two never play at the same time. A video without an archived
 * copy has no timeline to touch, so its anchors open the original instead.
 */
function RenderedContent({ payload, content, hideSmall, onImageContextMenu }: RenderedContentProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const canSeek = content.some(
    (block) =>
      (block.type === 'video' && isArchivedVideoUrl(block.url)) ||
      (block.type === 'audio' && Boolean(block.url)),
  );
  const originalHref = payload.article?.link || null;

  // A paragraph is on screen from its own `t` until the next one starts, so the
  // timeline only needs the sorted start times. Every paragraph carries one for a
  // transcript; the show notes above it do not, and never light up.
  const { starts, indexes } = useMemo(() => {
    const timed = content
      .map((block, index) => ({ index, t: block.type === 'paragraph' ? block.t : undefined }))
      .filter((item) => typeof item.t === 'number' && Number.isFinite(item.t));
    const sorted = [...timed].sort((a, b) => (a.t as number) - (b.t as number));
    return { starts: sorted.map((item) => item.t as number), indexes: sorted.map((item) => item.index) };
  }, [content]);

  // `timeupdate` fires about four times a second, which is too coarse for a short
  // paragraph but adequate for a highlight; the state only changes when the
  // paragraph does, so React re-renders about once a paragraph.
  const [activeIndex, setActiveIndex] = useState(-1);
  // Another article (or a refetched body) replaces every block, so the previous
  // index points at text that is no longer there. Adjusted during render, which is
  // where React wants state derived from a prop to be reset.
  const [renderedContent, setRenderedContent] = useState(content);
  if (renderedContent !== content) {
    setRenderedContent(content);
    setActiveIndex(-1);
  }
  const markAt = useCallback((seconds: number) => {
    // Last start at or before `seconds`; -1 before the first paragraph.
    let low = 0;
    let high = starts.length - 1;
    let found = -1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      if (starts[middle] <= seconds) {
        found = middle;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    const next = found === -1 ? -1 : indexes[found];
    setActiveIndex((previous) => (next === previous ? previous : next));
  }, [indexes, starts]);
  const onTimeUpdate = useCallback((event: React.SyntheticEvent<HTMLMediaElement>) => {
    markAt(event.currentTarget.currentTime);
  }, [markAt]);

  const controls = useMemo<PlayerControls>(() => {
    const pauseOther = (active: HTMLMediaElement) => {
      const other = active === videoRef.current ? audioRef.current : videoRef.current;
      if (other && !other.paused) other.pause();
    };

    return {
      canSeek,
      originalHref,
      pauseOther,
      seek: (seconds: number) => {
        const target: HTMLMediaElement | null = videoRef.current || audioRef.current;
        if (!target) return;
        pauseOther(target);
        target.currentTime = seconds;
        // The transcript follows the click at once instead of on the next
        // `timeupdate`, which keeps the highlight and the player in step.
        markAt(seconds);
        // Browsers may refuse the autoplay; the position is set, so the reader can
        // press play themselves. The rejection must still be consumed.
        void target.play().catch(() => {});
      },
    };
  }, [canSeek, markAt, originalHref]);
  const contentKeys = buildContentBlockKeys(content);
  let outlineIndex = 0;
  const outlineIndexes = content.map((block) => isOutlineBlock(block) ? outlineIndex++ : undefined);

  return (
    <PlayerContext.Provider value={controls}>
      {content.map((block, index) => (
        <ContentBlock
          key={contentKeys[index]}
          block={block}
          index={index}
          outlineIndex={outlineIndexes[index]}
          images={payload.images}
          hideSmall={hideSmall}
          onImageContextMenu={onImageContextMenu}
          audioRef={audioRef}
          videoRef={videoRef}
          onTimeUpdate={onTimeUpdate}
          isPlayingBlock={index === activeIndex}
        />
      ))}
    </PlayerContext.Provider>
  );
}
