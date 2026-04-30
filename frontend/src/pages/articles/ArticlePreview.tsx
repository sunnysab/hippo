import { useState, type RefObject } from 'react';
import { ContextMenu } from '../../components/ContextMenu';
import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { useReaderSettings } from '../../hooks/useReaderSettings';
import { ArticleHeader } from './ArticleHeader';
import { ArticleContent } from './ArticleContent';
import { EmptyState } from '../../components/EmptyState';
import { useToast } from '../../hooks/useToast';
import { apiGet, apiSend } from '../../api';

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

  const payload = state.currentArticlePayload;

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
        <div className="reader">
          <ArticleHeader article={payload.article} />
          <ArticleContent
            payload={payload}
            hideSmall={config.hideSmall}
            onImageContextMenu={setImageContextMenu}
          />
        </div>
      )}
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
