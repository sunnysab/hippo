import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { useReaderSettings } from '../../hooks/useReaderSettings';
import { ArticleHeader } from './ArticleHeader';
import { ArticleContent } from './ArticleContent';
import { EmptyState } from '../../components/EmptyState';
import { useToast } from '../../hooks/useToast';
import { apiSend } from '../../api';

export function ArticlePreview() {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const { config } = useReaderSettings();

  const payload = state.currentArticlePayload;

  // Handle image blocking from context menu
  const handleBlockImage = async (imageId: number) => {
    const container = document.getElementById('article-preview');
    const scrollTop = container ? container.scrollTop : 0;
    await apiSend(`/api/image/${imageId}/block`, 'POST', {});
    if (state.selectedArticleId) {
      const { apiGet } = await import('../../api');
      const newPayload = await apiGet(`/api/article/${state.selectedArticleId}`);
      dispatch({ type: 'SELECT_ARTICLE', id: state.selectedArticleId, payload: newPayload as unknown as typeof payload });
      if (container) container.scrollTop = scrollTop;
    }
    showToast(t('articles.imageBlocked', 'Image blocked.'));
  };

  return (
    <div className="article-preview-body" id="article-preview">
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
            onBlockImage={handleBlockImage}
          />
        </div>
      )}
    </div>
  );
}
