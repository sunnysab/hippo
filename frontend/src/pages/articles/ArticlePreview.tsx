import { lazy, Suspense, useState, useRef, useEffect, type RefObject } from 'react';
import { ContextMenu } from '../../components/ContextMenu';
import { CorrectionDialog } from '../../components/CorrectionDialog';
import { CORRECTIONS_UPDATED_EVENT } from '../../api/corrections';
import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { useReaderSettings } from '../../hooks/useReaderSettings';
import { ArticleHeader } from './ArticleHeader';
import { ArticleContent } from './ArticleContent';
import { HighlightToolbar } from './HighlightToolbar';
import { ReadingSidebar } from './ReadingSidebar';
import { buildOutline } from './outline';
import { useHighlights } from './useHighlights';
import { eligibleSelection, rawSelection } from './corrections';
import { EmptyState } from '../../components/EmptyState';
import { useToast } from '../../hooks/useToast';
import { apiGet, apiSend, isAuthError } from '../../api';
import type { ReadingPanel } from '../../hooks/useArticleReader';

const TableOfContents = lazy(() => import('./TableOfContents').then((module) => ({ default: module.TableOfContents })));

interface ArticlePreviewProps {
  previewRef: RefObject<HTMLDivElement | null>;
  sidePanel: ReadingPanel;
  onSidePanelToggle: (panel: Exclude<ReadingPanel, ''>) => void;
  /** Filter the list down to one story; omitted when the caller cannot filter. */
  onOpenEvent?: (eventId: number) => void;
  autoPlay?: boolean;
  onMediaEnded?: (articleId: number) => void;
  onMediaError?: (articleId: number) => void;
}

export function ArticlePreview({ previewRef, sidePanel, onSidePanelToggle, onOpenEvent, autoPlay, onMediaEnded, onMediaError }: ArticlePreviewProps) {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const { config } = useReaderSettings();
  const [imageContextMenu, setImageContextMenu] = useState<{
    imageId: number;
    x: number;
    y: number;
  } | null>(null);
  const [isBlockingImage, setIsBlockingImage] = useState(false);
  const readerRef = useRef<HTMLDivElement>(null);
  const [correction, setCorrection] = useState<{ articleId: number; source: string } | null>(null);
  const [dialogArticleId, setDialogArticleId] = useState(state.selectedArticleId);
  if (dialogArticleId !== state.selectedArticleId) {
    setDialogArticleId(state.selectedArticleId);
    setCorrection(null);
  }
  const correctionButtonRef = useRef<HTMLButtonElement>(null);
  const pendingSource = useRef<string | null>(null);
  const selectedRef = useRef(state.selectedArticleId);
  useEffect(() => { selectedRef.current = state.selectedArticleId; }, [state.selectedArticleId]);
  const selectedSource = () => {
    const selection = window.getSelection();
    const root = readerRef.current;
    if (!root || !selection || selection.isCollapsed || !selection.rangeCount) return '';
    const range = selection.getRangeAt(0);
    return root.contains(range.commonAncestorContainer) && eligibleSelection(range) ? rawSelection(root, range)?.quote ?? '' : '';
  };
  const openCorrection = (source: string) => {
    if (state.selectedArticleId) setCorrection({ articleId: state.selectedArticleId, source });
  };

  const [overlayPanel, setOverlayPanel] = useState(false);
  useEffect(() => {
    const container = previewRef.current;
    if (!container || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setOverlayPanel(entry.contentRect.width < 800);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [previewRef]);

  const payload = state.currentArticlePayload;
  const outline = buildOutline(payload?.content);
  const eligible = payload?.content?.some((block) => block.type === 'paragraph' && block.transcript_source === 'asr' && typeof block.original_text === 'string') ?? false;
  const contentKey = `${state.selectedArticleId ?? ''}:${payload?.content_updated_at ?? ''}:${JSON.stringify(payload?.content)}`;
  useEffect(() => {
    const id = state.selectedArticleId;
    if (!id) return;
    let active = true;
    let sequence = 0;
    const refresh = () => {
      const current = ++sequence;
      const scrollTop = previewRef.current?.scrollTop ?? 0;
      void apiGet(`/api/article/${id}`).then((result) => {
        if (!active || current !== sequence || selectedRef.current !== id) return;
        dispatch({ type: 'REFRESH_ARTICLE', id, payload: result as unknown as NonNullable<typeof payload> });
        requestAnimationFrame(() => { if (active && selectedRef.current === id && previewRef.current) previewRef.current.scrollTop = scrollTop; });
      }).catch((error) => { if (active && !isAuthError(error)) showToast((error as Error).message || t('articles.correctionRefreshFailed', 'Failed to refresh article.')); });
    };
    window.addEventListener(CORRECTIONS_UPDATED_EVENT, refresh);
    return () => { active = false; window.removeEventListener(CORRECTIONS_UPDATED_EVENT, refresh); };
  }, [state.selectedArticleId, dispatch, previewRef, showToast, t]);
  const { stale, create } = useHighlights({
    articleId: state.selectedArticleId ?? null,
    containerRef: readerRef,
    contentKey,
  });

  const handleBlockImage = async (imageId: number) => {
    const container = previewRef.current;
    const scrollTop = container?.scrollTop || 0;
    setIsBlockingImage(true);
    try {
      await apiSend(`/api/image/${imageId}/block`, 'POST', {});
      if (state.selectedArticleId) {
        const newPayload = await apiGet(`/api/article/${state.selectedArticleId}`);
        dispatch({
          type: 'SELECT_ARTICLE',
          id: state.selectedArticleId,
          payload: newPayload as unknown as typeof payload,
        });
        if (container) {
          container.scrollTop = scrollTop;
        }
      }
      showToast(t('articles.imageBlocked', 'Image blocked.'));
      setImageContextMenu(null);
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('articles.imageBlockFailed', 'Failed to block image.'));
    } finally {
      setIsBlockingImage(false);
    }
  };

  return (
    <div
      className={`article-preview-body${payload ? '' : ' is-empty'}${sidePanel && payload ? ' with-panel' : ''}${overlayPanel ? ' is-overlay-panel' : ''}`}
      id="article-preview"
      ref={previewRef}
    >
      {!payload ? (
        <div className="reader">
          <EmptyState message={t('articles.empty', 'Select an article to preview.')} />
        </div>
      ) : (
        <>
          {eligible ? <div className="toolbar article-correction-actions">
            <button ref={correctionButtonRef} type="button" className="btn ghost" onPointerDown={(event) => { pendingSource.current = selectedSource(); event.preventDefault(); }} onPointerCancel={() => { pendingSource.current = null; }} onMouseDown={(event) => event.preventDefault()} onClick={() => { openCorrection(pendingSource.current ?? selectedSource()); pendingSource.current = null; }}>{t('articles.addCorrection', 'Add correction')}</button>
            <button type="button" className="btn ghost" aria-pressed={state.originalView} onClick={() => dispatch({ type: 'TOGGLE_ARTICLE_ORIGINAL' })}>{state.originalView ? t('articles.showCorrected', 'Show corrected') : t('articles.showRaw', 'Show original')}</button>
          </div> : null}
        <div className="reader" ref={readerRef}>
          <ArticleHeader article={payload.article} onOpenEvent={onOpenEvent} />
          <ArticleContent
            autoPlay={autoPlay}
            onMediaEnded={onMediaEnded}
            onMediaError={onMediaError}
            payload={payload}
            hideSmall={config.hideSmall}
            followPlayback={config.followPlayback}
            miniPlayer={config.miniPlayer}
            onImageContextMenu={setImageContextMenu}
          />
          {stale.length > 0 ? (
            <section className="stale-highlights">
              <h3 className="stale-highlights-title">
                {t('articles.staleHighlights', '以下划线已无法定位（原文可能已变更）')}
              </h3>
              <ul className="stale-highlights-list">
                {stale.map((item) => (
                  <li key={item.id} className="stale-highlight">
                    <span className="stale-quote">{item.quote}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
        </>
      )}
      {payload ? (
        <>
          <HighlightToolbar containerRef={readerRef} onCreate={create} onCorrect={openCorrection} contentKey={contentKey} />
          <CorrectionDialog isOpen={correction?.articleId === state.selectedArticleId} onClose={() => setCorrection(null)} onSaved={() => {}} initialSource={correction?.source ?? ''} currentBiz={payload.article.biz} articleId={payload.article.id} returnFocusRef={correctionButtonRef} />
          {sidePanel === 'toc' ? (
            <Suspense fallback={<aside className='reader-toc' role='status'>{t('articles.outline.loading', 'Loading outline…')}</aside>}>
              <TableOfContents
                key={`${payload.article.id}:${payload.content_updated_at}`}
                items={outline}
                payload={payload}
                containerRef={previewRef}
                onClose={() => onSidePanelToggle('toc')}
              />
            </Suspense>
          ) : (
            <ReadingSidebar
              articleId={state.selectedArticleId ?? null}
              open={sidePanel === 'ai'}
              onToggle={() => onSidePanelToggle('ai')}
            />
          )}
        </>
      ) : null}
      <ContextMenu
        id="article-image-context-menu"
        isOpen={Boolean(imageContextMenu)}
        x={imageContextMenu?.x || 0}
        y={imageContextMenu?.y || 0}
        onClose={() => setImageContextMenu(null)}
      >
        <button
          className="context-item"
          id="article-image-menu-block"
          type="button"
          disabled={!imageContextMenu || isBlockingImage}
          onClick={() => {
            if (!imageContextMenu) return;
            void handleBlockImage(imageContextMenu.imageId);
          }}
        >
          {t('articles.menu.blockImage', 'Block image')}
        </button>
      </ContextMenu>
    </div>
  );
}
