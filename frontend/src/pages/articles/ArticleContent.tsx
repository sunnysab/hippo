import { createContext, memo, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useI18n } from '../../i18n';
import { renderInlineNodes } from '../../utils/markdown';
import { EmptyState } from '../../components/EmptyState';
import type { ArticlePayload, ArticleContentBlock } from '../../store/articles';
import { buildContentBlockKeys } from './contentKeys';
import { formatDuration as anchorLabel } from '../../utils/duration';
import { headingAnchorId } from './outline';

/**
 * A transcript is navigated by clicking an anchor in front of a paragraph, which
 * has to reach the player — and the player may be several blocks above. The reader
 * holds that little bit of state here instead of threading it through every block.
 */
interface PlayerControls {
  /** False when the article has no player, which is when anchors are plain text. */
  canSeek: boolean;
  seek: (seconds: number) => void;
}

const PlayerContext = createContext<PlayerControls>({ canSeek: false, seek: () => {} });

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

const ArticleVideoBlock = memo(function ArticleVideoBlock({ block }: { block: ArticleContentBlock }) {
  const { t } = useI18n();
  const [playing, setPlaying] = useState(false);

  if (!block.embed_url) return null;

  // The poster stays until the reader asks for the video: loading a third-party
  // player for every article would be a lot of JavaScript nobody watched.
  if (!playing) {
    return (
      <div className="reader-video" data-provider={block.provider || ''}>
        <button
          className="reader-video-poster"
          type="button"
          onClick={() => setPlaying(true)}
          aria-label={t('articles.playVideo', '播放视频')}
        >
          {block.cover_url ? <img src={block.cover_url} alt="" loading="lazy" /> : <span className="reader-video-placeholder" />}
          <span className="reader-video-play" aria-hidden="true">▶</span>
        </button>
      </div>
    );
  }

  return (
    <div className="reader-video">
      <iframe
        src={block.embed_url}
        title={block.video_id || t('articles.video', '视频')}
        allowFullScreen
        referrerPolicy="no-referrer"
        loading="lazy"
      />
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
  if (!block.url) return null;
  return (
    <div className="reader-audio">
      {/* No <track>: for a podcast the transcript below the player *is* the
          caption, and the feed does not ship one in a caption format. */}
      <audio ref={audioRef} controls preload='metadata' src={block.url} />
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
}: AnchorableBlockProps & { audioRef: React.RefObject<HTMLAudioElement | null> }) {
  const player = useContext(PlayerContext);

  if (block.type === 'paragraph') {
    const label = anchorLabel(block.t);
    const text = renderInlineNodes(block.text || '');
    if (!label) return <p>{text}</p>;
    if (!player.canSeek) {
      return (
        <p>
          <span className="reader-anchor" title={label}>{label}</span>
          {text}
        </p>
      );
    }
    return (
      <p>
        <button
          className="reader-anchor is-action"
          type="button"
          onClick={() => player.seek(block.t || 0)}
          title={label}
        >
          {label}
        </button>
        {text}
      </p>
    );
  }

  if (block.type === 'video') {
    return <ArticleVideoBlock block={block} />;
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
 * Renders the blocks and owns the one piece of cross-block state: the player the
 * transcript anchors seek. Audio seeks in place; a video has no timeline to touch
 * from here, so its anchors stay plain text until a provider-specific offset
 * parameter has been verified.
 */
function RenderedContent({ payload, content, hideSmall, onImageContextMenu }: RenderedContentProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const canSeek = content.some((block) => block.type === 'audio' && Boolean(block.url));
  const controls = useMemo<PlayerControls>(
    () => ({
      canSeek,
      seek: (seconds: number) => {
        const audio = audioRef.current;
        if (!audio) return;
        audio.currentTime = seconds;
        void audio.play().catch(() => {
          // 浏览器可能拒绝自动播放；位置已经设好，读者自己按播放键即可。
        });
      },
    }),
    [canSeek],
  );
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
        />
      ))}
    </PlayerContext.Provider>
  );
}
