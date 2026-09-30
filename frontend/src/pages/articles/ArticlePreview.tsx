import { useState, useRef, type RefObject } from 'react';
import { ContextMenu } from '../../components/ContextMenu';
import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { useReaderSettings } from '../../hooks/useReaderSettings';
import { ArticleHeader } from './ArticleHeader';
import { ArticleContent } from './ArticleContent';
import { HighlightToolbar } from './HighlightToolbar';
import { useHighlights } from './useHighlights';
import { EmptyState } from '../../components/EmptyState';
import { useToast } from '../../hooks/useToast';
import { apiGet, apiSend, isAuthError } from '../../api';

interface ArticlePreviewProps {
  previewRef: RefObject<HTMLDivElement | null>;
}

export function ArticlePreview({ previewRef }: ArticlePreviewProps) {
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

  const payload = state.currentArticlePayload;
  // The content key changes whenever the loaded article does, which is when the
  // rendered nodes are replaced and the marks must be rebuilt.
  const contentKey = String(state.selectedArticleId ?? '');
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
    <div className={`article-preview-body${payload ? '' : ' is-empty'}`} id="article-preview" ref={previewRef}>
      {!payload ? (
        <div className="reader">
          <EmptyState message={t('articles.empty', 'Select an article to preview.')} />
        </div>
      ) : (
        <div className="reader" ref={readerRef}>
          <ArticleHeader article={payload.article} />
          <ArticleContent
            payload={payload}
            hideSmall={config.hideSmall}
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
      )}
      {payload ? (
        <HighlightToolbar containerRef={readerRef} onCreate={create} />
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
