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
import { normalizeSafeUrl, renderInlineNodes, type InlineLinkRenderer } from '../../utils/markdown';
import { ArticleReferenceLink } from './ArticleReferenceLink';
import { useArticleReferences } from './useArticleReferences';
import { EmptyState } from '../../components/EmptyState';
import { apiGet, apiSend, isAuthError } from '../../api';
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
  /** True while the reader listens to the video's audio instead of watching it. */
  audioOnly: boolean;
  /** The switch is worth drawing: there is something to listen to, or to prepare. */
  showModeButton: boolean;
  /** An extraction is running, so the switch is busy rather than pressable. */
  preparingAudio: boolean;
  toggleMode: () => void;
  /** The mounted player is running, which is what the dock's button flips. */
  playing: boolean;
  togglePlay: () => void;
  /** Brings the paragraph that is playing back into view, and resumes following. */
  backToParagraph: () => void;
  /** The reader scrolled away from the playing paragraph, so the dock is shown. */
  showDock: boolean;
  /** True while the video floats over the page instead of sitting in the text. */
  pip: boolean;
  /** Where the floating video was dragged to, as a distance from its corner. */
  pipPos: { right: number; bottom: number } | null;
  /** Returns the video to its block, and closes the mini player for good. */
  returnToVideo: () => void;
  /** Hides the mini player until the video scrolls back into view by itself. */
  closePip: () => void;
  startPipDrag: (event: React.PointerEvent<HTMLElement>) => void;
}

// Only render-time values live here; the two media refs travel down as props so
// that no ref is touched while a block renders.
const PlayerContext = createContext<PlayerControls>({
  canSeek: false,
  seek: () => {},
  pauseOther: () => {},
  originalHref: null,
  audioOnly: false,
  showModeButton: false,
  preparingAudio: false,
  toggleMode: () => {},
  playing: false,
  togglePlay: () => {},
  backToParagraph: () => {},
  showDock: false,
  pip: false,
  pipPos: null,
  returnToVideo: () => {},
  closePip: () => {},
  startPipDrag: () => {},
});

/**
 * The archived-video path the backend hands out is exactly `/api/media/<id>/video`.
 * Anything else is not our media: a protocol URL, `//host`, `/arbitrary`, or a
 * backslash the browser rewrites into a remote host (`/\evil.test`).
 */
const ARCHIVED_VIDEO_PATH = /^\/api\/media\/[1-9]\d*\/video$/;

/** How long to wait for an extraction before giving up on the switch. */
const AUDIO_POLL_INTERVAL_MS = 3000;
const AUDIO_POLL_ATTEMPTS = 20;

const isArchivedVideoUrl = (url: string | undefined): url is string =>
  typeof url === 'string' && ARCHIVED_VIDEO_PATH.test(url);

/**
 * The nearest ancestor that scrolls, which is what "still on screen" has to be
 * measured against: the reader scrolls inside its own pane, not the document, so
 * a viewport test would call a clipped paragraph visible.
 */
const scrollPane = (node: Element | null): Element | null => {
  for (let el = node?.parentElement ?? null; el; el = el.parentElement) {
    if (/auto|scroll/.test(getComputedStyle(el).overflowY)) return el;
  }
  return null;
};

/** Keys a reader scrolls with; pressing one means the transcript is being steered. */
const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ']);

interface ArticleContentProps {
  payload: ArticlePayload;
  hideSmall: boolean;
  /** Scroll the playing paragraph into view as the player moves on. */
  followPlayback: boolean;
  /** Float the video over the page once it is scrolled out of sight. */
  miniPlayer: boolean;
  onImageContextMenu: (detail: { imageId: number; x: number; y: number }) => void;
}

interface ContentBlockProps {
  renderLink: InlineLinkRenderer;
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
  renderLink,
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

  const image = (
    <img
      key={`image-${block.image_id}`}
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
  );
  const href = normalizeSafeUrl(block.href || '');
  return (
    <figure ref={figureRef} data-image-id={String(block.image_id)}>
      {href ? renderLink(href, [image], `image-${block.image_id}`) : image}
    </figure>
  );
});

interface PlayerRefProps {
  audioRef: React.RefObject<HTMLAudioElement | null>;
  videoRef: React.RefObject<HTMLVideoElement | null>;
  /** Reports the player's position so the transcript can follow along. */
  onTimeUpdate: (event: React.SyntheticEvent<HTMLMediaElement>) => void;
  /** Called once the other player has loaded, to take over position and playback. */
  onReady: (event: React.SyntheticEvent<HTMLMediaElement>) => void;
  /** Reports a play, a pause and an end alike, so the dock knows if sound is running. */
  onPlayback: (playing: boolean, player: HTMLMediaElement) => void;
}

/**
 * The "listen only" switch. It sits under whichever player is on screen, and both
 * of them render it: switching to the audio unmounts the video, so a control that
 * lived with only one of them would disappear the moment it was used.
 */
const PlayerModeButton = memo(function PlayerModeButton() {
  const { t } = useI18n();
  const { audioOnly, toggleMode, preparingAudio } = useContext(PlayerContext);
  if (audioOnly) {
    return (
      <button className="reader-audio-toggle" type="button" onClick={toggleMode}>
        {t('articles.showVideo', '看视频')}
      </button>
    );
  }
  return (
    <button
      className="reader-audio-toggle"
      type="button"
      disabled={preparingAudio}
      onClick={toggleMode}
      title={t('articles.listenOnlyHint', '只播放声音，不下载视频。')}
    >
      {preparingAudio
        ? t('articles.preparingAudio', '正在准备音频…')
        : t('articles.listenOnly', '仅音频')}
    </button>
  );
});

const ArticleVideoBlock = memo(function ArticleVideoBlock({
  block,
  videoRef,
  onTimeUpdate,
  onReady,
  onPlayback,
}: {
  block: ArticleContentBlock;
  videoRef: React.RefObject<HTMLVideoElement | null>;
  onTimeUpdate: PlayerRefProps['onTimeUpdate'];
  onReady: PlayerRefProps['onReady'];
  onPlayback: PlayerRefProps['onPlayback'];
}) {
  const { t } = useI18n();
  const {
    originalHref, pauseOther, showModeButton, pip, pipPos, playing, togglePlay,
    backToParagraph, returnToVideo, closePip, startPipDrag,
  } = useContext(PlayerContext);

  // The archived copy streams from Hippo with Range support, so it plays at once
  // and never contacts the provider — even when a legacy block still carries an
  // `embed_url`. The floating player is the same element wearing another class:
  // moving it in the DOM would stop the sound and lose the position.
  if (isArchivedVideoUrl(block.url)) {
    return (
      <>
        {/* Fixed takes the frame out of the text flow; the slot keeps its height so
            the transcript does not jump when the video floats away. */}
        {pip ? <div className='reader-video-slot' aria-hidden='true' /> : null}
        <div
          className={`reader-video${pip ? ' is-pip' : ''}`}
          data-provider={block.provider || ''}
          style={pip && pipPos ? { right: pipPos.right, bottom: pipPos.bottom } : undefined}
        >
          <video
            ref={videoRef}
            src={block.url}
            controls
            preload='metadata'
            playsInline
            poster={block.cover_url || undefined}
            onPlay={(event) => {
              pauseOther(event.currentTarget);
              onTimeUpdate(event);
              onPlayback(true, event.currentTarget);
            }}
            onPause={(event) => onPlayback(false, event.currentTarget)}
            onEnded={(event) => onPlayback(false, event.currentTarget)}
            onTimeUpdate={onTimeUpdate}
            onLoadedMetadata={onReady}
            data-testid='reader-video-file'
          />
          {pip ? (
            // The bar sits outside the picture: the native controls need every
            // pixel of the video itself, and a drag surface over them would eat
            // the seek bar.
            <div className='reader-pip-bar'>
              <span
                className='reader-pip-grip'
                title={t('articles.pipDragHint', '拖动移动小窗')}
                onPointerDown={startPipDrag}
                aria-hidden='true'
              >
                <svg viewBox='0 0 24 24'>
                  <circle cx='9' cy='7' r='1.4' /><circle cx='15' cy='7' r='1.4' />
                  <circle cx='9' cy='12' r='1.4' /><circle cx='15' cy='12' r='1.4' />
                  <circle cx='9' cy='17' r='1.4' /><circle cx='15' cy='17' r='1.4' />
                </svg>
              </span>
              <button
                className='reader-pip-btn is-icon'
                type='button'
                onClick={togglePlay}
                aria-label={playing ? t('articles.pause', '暂停') : t('articles.play', '播放')}
                title={playing ? t('articles.pause', '暂停') : t('articles.play', '播放')}
              >
                {playing ? <PauseGlyph /> : <PlayGlyph />}
              </button>
              <button className='reader-pip-btn' type='button' onClick={backToParagraph}>
                {t('articles.backToParagraph', '回到当前')}
              </button>
              <button className='reader-pip-btn' type='button' onClick={returnToVideo}>
                {t('articles.backToVideo', '回到视频')}
              </button>
              <button
                className='reader-pip-btn is-icon'
                type='button'
                onClick={closePip}
                aria-label={t('articles.closePip', '关闭小窗')}
                title={t('articles.closePip', '关闭小窗')}
              >
                <svg viewBox='0 0 24 24' aria-hidden='true'>
                  <path d='M6 6l12 12M18 6L6 18' />
                </svg>
              </button>
            </div>
          ) : showModeButton ? (
            <PlayerModeButton />
          ) : null}
        </div>
      </>
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
  onReady,
  onPlayback,
}: {
  block: ArticleContentBlock;
  audioRef: React.RefObject<HTMLAudioElement | null>;
  onTimeUpdate: PlayerRefProps['onTimeUpdate'];
  onReady: PlayerRefProps['onReady'];
  onPlayback: PlayerRefProps['onPlayback'];
}) {
  const { pauseOther, showModeButton } = useContext(PlayerContext);
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
        onPlay={(event) => {
          pauseOther(event.currentTarget);
          onTimeUpdate(event);
          onPlayback(true, event.currentTarget);
        }}
        onPause={(event) => onPlayback(false, event.currentTarget)}
        onEnded={(event) => onPlayback(false, event.currentTarget)}
        onTimeUpdate={onTimeUpdate}
        onLoadedMetadata={onReady}
      />
      {showModeButton ? <PlayerModeButton /> : null}
    </div>
  );
});

const PlayGlyph = memo(function PlayGlyph() {
  return (
    <span className='icon' aria-hidden='true'>
      <svg viewBox='0 0 24 24'><path d='M8 5v14l11-7z' /></svg>
    </span>
  );
});

const PauseGlyph = memo(function PauseGlyph() {
  return (
    <span className='icon' aria-hidden='true'>
      <svg viewBox='0 0 24 24'><path d='M6 5h4v14H6zm8 0h4v14h-4z' /></svg>
    </span>
  );
});

/**
 * The player's controls, shown while the reader is somewhere other than the
 * paragraph that is playing. Two buttons, because those are the two things that
 * are then out of reach: sound, and the text that goes with it.
 */
const PlayerDock = memo(function PlayerDock() {
  const { t } = useI18n();
  const { playing, togglePlay, backToParagraph, showDock } = useContext(PlayerContext);
  if (!showDock) return null;
  return (
    <div className='reader-dock' role='group' aria-label={t('articles.playerControls', '播放控制')}>
      <button
        className='reader-dock-btn'
        type='button'
        onClick={togglePlay}
        aria-label={playing ? t('articles.pause', '暂停') : t('articles.play', '播放')}
        title={playing ? t('articles.pause', '暂停') : t('articles.play', '播放')}
      >
        {playing ? <PauseGlyph /> : <PlayGlyph />}
      </button>
      <button className='reader-dock-btn is-text' type='button' onClick={backToParagraph}>
        {t('articles.backToParagraph', '回到当前')}
      </button>
    </div>
  );
});

const ContentBlock = memo(function ContentBlock({
  block,
  renderLink,
  index,
  images,
  hideSmall,
  onImageContextMenu,
  audioRef,
  videoRef,
  onTimeUpdate,
  onReady,
  onPlayback,
  isPlayingBlock,
  outlineIndex,
  playingRef,
  hasAudioBlock,
  fallbackAudioUrl,
}: AnchorableBlockProps & PlayerRefProps & {
  isPlayingBlock: boolean;
  playingRef: React.RefObject<HTMLParagraphElement | null>;
  /** The body already carries an audio block, so nothing has to be synthesised. */
  hasAudioBlock: boolean;
  /** An audio file the body does not mention (extracted from an older video). */
  fallbackAudioUrl: string | null;
}) {
  const { canSeek, seek, originalHref, audioOnly, showModeButton } = useContext(PlayerContext);

  const anchor = { 'data-outline-block': outlineIndex };

  if (['paragraph', 'text', 'link'].includes(block.type)) {
    const label = anchorLabel(block.t);
    const href = block.type === 'link' ? normalizeSafeUrl(block.href || '') : null;
    const text = href
      ? renderLink(href, renderInlineNodes(block.text || href, (_href, label) => label), `block-${index}`)
      : renderInlineNodes(block.text || '', renderLink);
    const className = isPlayingBlock ? 'is-playing' : undefined;
    const playingProps = isPlayingBlock ? { ref: playingRef } : {};
    if (!label) return <p {...anchor} {...playingProps} className={className}>{text}</p>;
    if (!canSeek) {
      // Nothing is archived here, so the anchor opens the original at that moment
      // instead of seeking a player that does not exist.
      const href = originalHref ? timestampHref(originalHref, block.t || 0) : null;
      if (!href) {
        return (
          <p {...anchor} {...playingProps} className={className}>
            <span className="reader-anchor" title={label}>{label}</span>
            {text}
          </p>
        );
      }
      return (
        <p {...anchor} {...playingProps} className={className}>
          <a className="reader-anchor is-action" href={href} target="_blank" rel="noreferrer" title={label}>
            {label}
          </a>
          {text}
        </p>
      );
    }
    return (
      <p {...anchor} {...playingProps} className={className}>
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
    // Only one player is mounted at a time: the one not chosen is not downloaded
    // at all, which is the whole point of the audio-only mode.
    if (audioOnly && showModeButton) {
      // An older article has no audio block in its body, so the player the reader
      // switched to is built here, where the video was.
      if (hasAudioBlock || !fallbackAudioUrl) return null;
      return (
        <ArticleAudioBlock
          block={{ type: 'audio', url: fallbackAudioUrl }}
          audioRef={audioRef}
          onTimeUpdate={onTimeUpdate}
          onReady={onReady}
          onPlayback={onPlayback}
        />
      );
    }
    return (
      <ArticleVideoBlock
        block={block}
        videoRef={videoRef}
        onTimeUpdate={onTimeUpdate}
        onReady={onReady}
        onPlayback={onPlayback}
      />
    );
  }

  if (block.type === 'audio') {
    if (!audioOnly && showModeButton) return null;
    return (
      <ArticleAudioBlock
        block={block}
        audioRef={audioRef}
        onTimeUpdate={onTimeUpdate}
        onReady={onReady}
        onPlayback={onPlayback}
      />
    );
  }

  if (block.type === 'heading') {
    // The id is what the outline links to; it must match `buildOutline`.
    const id = headingAnchorId(index);
    const level = Math.min(Math.max(Number(block.level) || 2, 2), 4);
    if (level === 2) {
      return <h2 {...anchor} id={id}>{renderInlineNodes(block.text || '', renderLink)}</h2>;
    }
    if (level === 3) {
      return <h3 {...anchor} id={id}>{renderInlineNodes(block.text || '', renderLink)}</h3>;
    }
    return <h4 {...anchor} id={id}>{renderInlineNodes(block.text || '', renderLink)}</h4>;
  }

  if (block.type === 'image') {
    return (
      <ArticleImageBlock
        block={block}
        renderLink={renderLink}
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

export function ArticleContent({
  payload,
  hideSmall,
  followPlayback,
  miniPlayer,
  onImageContextMenu,
}: ArticleContentProps) {
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
      followPlayback={followPlayback}
      miniPlayer={miniPlayer}
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
function RenderedContent({
  payload,
  content,
  hideSmall,
  followPlayback,
  miniPlayer,
  onImageContextMenu,
}: RenderedContentProps) {
  const { references, failed: referenceFailed, retry: retryReference } = useArticleReferences(payload);
  const renderLink = useCallback<InlineLinkRenderer>((href, label, key) => {
    const normalized = href.replace(/^http:/, 'https:').replace(/&amp;/g, '&');
    return <ArticleReferenceLink key={key} href={href} label={label} reference={references.get(normalized)}
      failed={referenceFailed} onRetry={retryReference} />;
  }, [referenceFailed, references, retryReference]);
  const audioRef = useRef<HTMLAudioElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const playingRef = useRef<HTMLParagraphElement>(null);
  const canSeek = content.some(
    (block) =>
      (block.type === 'video' && isArchivedVideoUrl(block.url)) ||
      (block.type === 'audio' && Boolean(block.url)),
  );
  const originalHref = payload.article?.link || null;

  // A video article keeps both a watchable MP4 and a stripped-down mp3, and the
  // reader usually only wants the second. The choice is per open article, never
  // stored: opening something new starts with the picture, as before.
  const hasVideo = content.some((block) => block.type === 'video' && isArchivedVideoUrl(block.url));
  const archivedAudio = content.find((block) => block.type === 'audio' && Boolean(block.url))?.url || null;
  // Articles archived before the audio track was kept have the video only; the
  // server can still strip one out, and this is what it produced when asked.
  const [fetchedAudio, setFetchedAudio] = useState<string | null>(null);
  const audioUrl = archivedAudio || fetchedAudio;
  const [audioOnly, setAudioOnly] = useState(false);
  const [preparingAudio, setPreparingAudio] = useState(false);
  const articleId = payload.article?.id ?? null;
  // Where the player was when the reader switched. The other element starts empty,
  // so it is told where to go once its metadata has arrived. The speed travels with
  // it: a fresh element always starts at 1x, and losing a 2x listen is losing the
  // reader's choice.
  const handoverRef = useRef<{ time: number; playing: boolean; rate: number } | null>(null);

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

  const [activeIndex, setActiveIndex] = useState(-1);
  // Following is a claim on the reader's scroll position, so it lasts only until
  // the reader takes it back: a scroll of their own stops the auto-scroll while the
  // highlight keeps moving, and the dock offers the way back.
  const [following, setFollowing] = useState(true);
  // Sound is running somewhere; the dock's and the mini player's button flips this.
  const [playing, setPlaying] = useState(false);
  // A video that has been playing keeps its mini player through a pause: pausing
  // from the floating frame is one of the things it is there for.
  const [started, setStarted] = useState(false);
  // The mini player is dismissed by hand or by scrolling back, per article.
  const [pipDismissed, setPipDismissed] = useState(false);
  const [pipPos, setPipPos] = useState<{ right: number; bottom: number } | null>(null);
  // Another article (or a refetched body) replaces every block, so the previous
  // index points at text that is no longer there. Adjusted during render, which is
  // where React wants state derived from a prop to be reset.
  const [renderedContent, setRenderedContent] = useState(content);
  if (renderedContent !== content) {
    setRenderedContent(content);
    setActiveIndex(-1);
    setAudioOnly(false);
    setFetchedAudio(null);
    setPreparingAudio(false);
    setFollowing(true);
    setPlaying(false);
    setStarted(false);
    setPipDismissed(false);
    setPipPos(null);
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
  // `timeupdate` is measured in wall-clock time and fires about four times a
  // second, but it advances the media position by `playbackRate / 4` seconds. At
  // 4x the highlight would trail the sound by a whole second, so while the player
  // runs the clock is read every frame instead. The loop stops itself once the
  // player pauses: nothing ticks while a transcript is merely being read.
  const clockRef = useRef<HTMLMediaElement | null>(null);
  const frameRef = useRef(0);
  // The loop outlives the render that started it, so it reads the newest
  // timeline through a ref instead of the closure it was created in.
  const markRef = useRef(markAt);
  useEffect(() => { markRef.current = markAt; }, [markAt]);
  const onTimeUpdate = useCallback((event: React.SyntheticEvent<HTMLMediaElement>) => {
    const player = event.currentTarget;
    markRef.current(player.currentTime);
    clockRef.current = player;
    if (frameRef.current) return;
    const tick = () => {
      const current = clockRef.current;
      // A player swapped out (audio-only mode, another article) may still be
      // playing while detached, and its clock no longer belongs to this timeline.
      if (!current || current.paused || !current.isConnected) {
        frameRef.current = 0;
        return;
      }
      markRef.current(current.currentTime);
      frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);
  }, []);
  useEffect(() => () => cancelAnimationFrame(frameRef.current), []);

  // Follow the player: the highlight is useless if it has scrolled out of sight.
  // Keyed on the index, so this runs once per paragraph rather than on every
  // `timeupdate`, and never while the reader is steering. `followRequest` is the
  // explicit "take me back" that has to fire even when following never stopped:
  // both counters change in the same render, so it is still one scroll.
  const [followRequest, setFollowRequest] = useState(0);
  useEffect(() => {
    if (!followPlayback || !following || activeIndex < 0) return;
    playingRef.current?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
  }, [activeIndex, followPlayback, following, followRequest]);

  // A scroll the reader caused (wheel, drag, key, touch) has to end the following,
  // but a scroll this component caused must not. `scroll` carries no cause, so the
  // intent is taken from the input that precedes it instead. Only the reading pane
  // counts: scrolling the article list must not steal the transcript's follow.
  useEffect(() => {
    if (!playing) return;
    const pane = scrollPane(playingRef.current ?? audioRef.current ?? videoRef.current);
    const inPane = (event: Event) => !pane || (event.target instanceof Node && pane.contains(event.target));
    const stop = (event: Event) => { if (inPane(event)) setFollowing(false); };
    // Dragging the pane's own scrollbar is a scroll; pressing a link or selecting
    // text is not, and must not take the follow away.
    const onPointerDown = (event: Event) => { if (event.target === pane) setFollowing(false); };
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || !SCROLL_KEYS.has(event.key)) return;
      if (event.target instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
      setFollowing(false);
    };
    document.addEventListener('wheel', stop, { passive: true });
    document.addEventListener('touchstart', stop, { passive: true });
    document.addEventListener('pointerdown', onPointerDown, { passive: true });
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('wheel', stop);
      document.removeEventListener('touchstart', stop);
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [playing]);

  // The dock exists for the case where the text has left the screen: while the
  // playing paragraph is visible the reader has everything already. It outlives a
  // pause on purpose -- pausing from the dock is the whole point of drawing it, and
  // a control that vanishes the moment it is used cannot be pressed twice.
  const [paragraphGone, setParagraphGone] = useState(false);
  // True once the video has been scrolled out of the reading pane while it runs.
  const [videoGone, setVideoGone] = useState(false);
  useEffect(() => {
    if (activeIndex < 0 && !hasVideo) return;
    const pane = scrollPane(playingRef.current ?? videoRef.current ?? audioRef.current);
    const box = () => (pane ?? document.documentElement).getBoundingClientRect();
    // An unlaid-out box (a hidden pane, a test that has no layout) says nothing
    // about visibility, so it never counts as "scrolled away".
    const offscreen = (node: HTMLElement | null) => {
      if (!node) return false;
      const rect = node.getBoundingClientRect();
      const view = box();
      return rect.height > 0 && (rect.bottom <= view.top || rect.top >= view.bottom);
    };
    // The floating frame reports its own fixed position, which is always on
    // screen: measuring it would switch the mini player off the moment it appeared
    // and back on again on the next frame. The slot holds the video's place in the
    // text and does not move, so it is what gets measured.
    const videoHome = () => {
      const frame = videoRef.current?.closest<HTMLElement>('.reader-video') ?? null;
      if (!frame) return null;
      const slot = frame.previousElementSibling;
      return slot?.classList.contains('reader-video-slot') ? (slot as HTMLElement) : frame;
    };
    let frame = 0;
    const update = () => {
      frame = 0;
      setParagraphGone(activeIndex >= 0 && offscreen(playingRef.current));
      setVideoGone(offscreen(videoHome()));
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    // The first measurement lands in a frame too, which is what keeps the effect
    // from setting state during the commit that mounted it.
    schedule();
    (pane ?? window).addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
    for (const node of [playingRef.current, videoHome()]) {
      if (node) observer?.observe(node);
    }
    return () => {
      cancelAnimationFrame(frame);
      (pane ?? window).removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      observer?.disconnect();
    };
  }, [activeIndex, hasVideo, audioOnly]);

  // The mini player: the video floats over the page once it has been scrolled out
  // of sight while it plays. It is dismissed by hand or by scrolling back, and both
  // ways are per article -- nothing here is remembered beyond the open payload.
  const pip = miniPlayer && hasVideo && !audioOnly && started && videoGone && !pipDismissed;
  // Two floats in the same corner would sit on top of each other, and the frame
  // carries its own controls while it is there.
  const showDock = activeIndex >= 0 && paragraphGone && !pip;
  // Coming back into view is the reset: the next scroll away may float it again.
  // Adjusted during render, the same way the previous article's state is cleared.
  const [pipWasGone, setPipWasGone] = useState(videoGone);
  if (pipWasGone !== videoGone) {
    setPipWasGone(videoGone);
    if (!videoGone) setPipDismissed(false);
  }
  const returnToVideo = useCallback(() => {
    setPipDismissed(true);
    videoRef.current?.closest<HTMLElement>('.reader-video')
      ?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
  }, []);
  const startPipDrag = useCallback((event: React.PointerEvent<HTMLElement>) => {
    const frame = videoRef.current?.closest<HTMLElement>('.reader-video');
    if (!frame) return;
    const rect = frame.getBoundingClientRect();
    const originX = event.clientX;
    const originY = event.clientY;
    const start = { right: window.innerWidth - rect.right, bottom: window.innerHeight - rect.bottom };
    const grip = event.currentTarget;
    grip.setPointerCapture?.(event.pointerId);
    const move = (moveEvent: PointerEvent) => {
      // Clamp so the frame cannot be dragged off the screen and lost.
      setPipPos({
        right: Math.min(Math.max(start.right + originX - moveEvent.clientX, 8), Math.max(window.innerWidth - rect.width - 8, 8)),
        bottom: Math.min(Math.max(start.bottom + originY - moveEvent.clientY, 8), Math.max(window.innerHeight - rect.height - 8, 8)),
      });
    };
    const stop = () => {
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', stop);
      grip.removeEventListener('pointercancel', stop);
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', stop);
    grip.addEventListener('pointercancel', stop);
  }, []);

  // Older video articles have no audio track to switch to. The server extracts one
  // from the archived copy, and this polls until it is there instead of making the
  // reader press the button again.
  const prepareAudio = useCallback(async () => {
    if (articleId === null) return;
    setPreparingAudio(true);
    try {
      for (let attempt = 0; attempt < AUDIO_POLL_ATTEMPTS; attempt += 1) {
        const answer = attempt === 0
          ? await apiSend(`/api/article/${articleId}/audio`, 'POST', {})
          : await apiGet(`/api/article/${articleId}/audio`, { silent: true });
        const state = String(answer.state || '');
        if (state === 'stored' && typeof answer.url === 'string') {
          setFetchedAudio(answer.url);
          setAudioOnly(true);
          return;
        }
        if (state === 'unavailable' || state === 'failed') break;
        await new Promise((resolve) => setTimeout(resolve, AUDIO_POLL_INTERVAL_MS));
      }
    } catch (error) {
      if (isAuthError(error)) return;
    } finally {
      setPreparingAudio(false);
    }
  }, [articleId]);

  // Whichever element is on screen right now. Only one is ever mounted, and a
  // podcast mounts the audio without any video in the body.
  const mounted = useCallback(
    () => (audioOnly ? audioRef.current : videoRef.current || audioRef.current),
    [audioOnly],
  );

  // The dock and the players share one notion of "running", whichever element is
  // mounted at the time: a switch to audio-only or back hands it over.
  const onPlayback = useCallback((running: boolean, player: HTMLMediaElement) => {
    // A player the reader switched away from must not report a pause over the one
    // that replaced it, which is already running.
    if (!running && player !== mounted()) return;
    if (running) setStarted(true);
    setPlaying(running);
  }, [mounted]);

  const onReady = useCallback((event: React.SyntheticEvent<HTMLMediaElement>) => {
    const handover = handoverRef.current;
    if (!handover) return;
    handoverRef.current = null;
    event.currentTarget.currentTime = handover.time;
    event.currentTarget.playbackRate = handover.rate;
    if (handover.playing) void event.currentTarget.play().catch(() => {});
  }, []);

  const controls = useMemo<PlayerControls>(() => {
    const pauseOther = (active: HTMLMediaElement) => {
      const other = active === videoRef.current ? audioRef.current : videoRef.current;
      if (other && !other.paused) other.pause();
    };

    return {
      canSeek,
      originalHref,
      audioOnly,
      showModeButton: hasVideo,
      preparingAudio,
      toggleMode: () => {
        const current = audioOnly ? audioRef.current : videoRef.current;
        handoverRef.current = current
          ? { time: current.currentTime, playing: !current.paused, rate: current.playbackRate }
          : null;
        if (current && !current.paused) current.pause();
        if (!audioOnly && !audioUrl) {
          // Nothing to switch to yet: extract first, and turn the player over once
          // the file exists.
          void prepareAudio();
          return;
        }
        setAudioOnly((previous) => !previous);
      },
      playing,
      togglePlay: () => {
        const target = mounted();
        if (!target) return;
        if (target.paused) void target.play().catch(() => {});
        else target.pause();
      },
      backToParagraph: () => {
        // Asking for the paragraph is asking to be followed again.
        setFollowing(true);
        setFollowRequest((previous) => previous + 1);
      },
      showDock,
      pip,
      pipPos,
      returnToVideo,
      closePip: () => setPipDismissed(true),
      startPipDrag,
      pauseOther,
      seek: (seconds: number) => {
        const target: HTMLMediaElement | null = videoRef.current || audioRef.current;
        if (!target) return;
        pauseOther(target);
        target.currentTime = seconds;
        // The transcript follows the click at once instead of on the next
        // `timeupdate`, which keeps the highlight and the player in step.
        markAt(seconds);
        // An anchor is the reader asking for that paragraph, so following resumes.
        setFollowing(true);
        // Browsers may refuse the autoplay; the position is set, so the reader can
        // press play themselves. The rejection must still be consumed.
        void target.play().catch(() => {});
      },
    };
  }, [
    audioOnly, audioUrl, canSeek, hasVideo, markAt, mounted, originalHref, pip, pipPos,
    playing, prepareAudio, preparingAudio, returnToVideo, showDock, startPipDrag,
  ]);
  const contentKeys = buildContentBlockKeys(content);
  let outlineIndex = 0;
  const outlineIndexes = content.map((block) => isOutlineBlock(block) ? outlineIndex++ : undefined);

  return (
    <PlayerContext.Provider value={controls}>
      {content.map((block, index) => (
        <ContentBlock
          key={contentKeys[index]}
          block={block}
          renderLink={renderLink}
          index={index}
          outlineIndex={outlineIndexes[index]}
          images={payload.images}
          hideSmall={hideSmall}
          onImageContextMenu={onImageContextMenu}
          audioRef={audioRef}
          videoRef={videoRef}
          onTimeUpdate={onTimeUpdate}
          onReady={onReady}
          onPlayback={onPlayback}
          isPlayingBlock={index === activeIndex}
          playingRef={playingRef}
          hasAudioBlock={Boolean(archivedAudio)}
          fallbackAudioUrl={audioUrl}
        />
      ))}
      <PlayerDock />
    </PlayerContext.Provider>
  );
}
