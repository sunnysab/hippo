import { createContext, memo, useContext, useEffect, useMemo, useRef } from 'react';
import { useI18n } from '../../i18n';
import { renderInlineNodes } from '../../utils/markdown';
import { EmptyState } from '../../components/EmptyState';
import type { ArticlePayload, ArticleContentBlock } from '../../store/articles';
import { buildContentBlockKeys } from './contentKeys';
import { formatDuration as anchorLabel, timestampHref } from '../../utils/duration';
import { headingAnchorId } from './outline';

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
}

const ArticleVideoBlock = memo(function ArticleVideoBlock({
  block,
  videoRef,
}: {
  block: ArticleContentBlock;
  videoRef: React.RefObject<HTMLVideoElement | null>;
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
}: {
  block: ArticleContentBlock;
  audioRef: React.RefObject<HTMLAudioElement | null>;
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
}: AnchorableBlockProps & PlayerRefProps) {
  const { canSeek, seek, originalHref } = useContext(PlayerContext);

  if (block.type === 'paragraph') {
    const label = anchorLabel(block.t);
    const text = renderInlineNodes(block.text || '');
    if (!label) return <p>{text}</p>;
    if (!canSeek) {
      // Nothing is archived here, so the anchor opens the original at that moment
      // instead of seeking a player that does not exist.
      const href = originalHref ? timestampHref(originalHref, block.t || 0) : null;
      if (!href) {
        return (
          <p>
            <span className="reader-anchor" title={label}>{label}</span>
            {text}
          </p>
        );
      }
      return (
        <p>
          <a className="reader-anchor is-action" href={href} target="_blank" rel="noreferrer" title={label}>
            {label}
          </a>
          {text}
        </p>
      );
    }
    return (
      <p>
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
    return <ArticleVideoBlock block={block} videoRef={videoRef} />;
  }

  if (block.type === 'audio') {
    return <ArticleAudioBlock block={block} audioRef={audioRef} />;
  }

  if (block.type === 'heading') {
    // The id is what the outline links to; it must match `buildOutline`.
    const id = headingAnchorId(index);
    const level = Math.min(Math.max(Number(block.level) || 2, 2), 4);
    if (level === 2) {
      return <h2 id={id}>{renderInlineNodes(block.text || '')}</h2>;
    }
    if (level === 3) {
      return <h3 id={id}>{renderInlineNodes(block.text || '')}</h3>;
    }
    return <h4 id={id}>{renderInlineNodes(block.text || '')}</h4>;
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
      <pre className="code-block"><code>{codeText}</code></pre>
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
        // Browsers may refuse the autoplay; the position is set, so the reader can
        // press play themselves. The rejection must still be consumed.
        void target.play().catch(() => {});
      },
    };
  }, [canSeek, originalHref]);
  const contentKeys = buildContentBlockKeys(content);

  return (
    <PlayerContext.Provider value={controls}>
      {content.map((block, index) => (
        <ContentBlock
          key={contentKeys[index]}
          block={block}
          index={index}
          images={payload.images}
          hideSmall={hideSmall}
          onImageContextMenu={onImageContextMenu}
          audioRef={audioRef}
          videoRef={videoRef}
        />
      ))}
    </PlayerContext.Provider>
  );
}
