import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { useArticleFilters } from '../../hooks/useArticleFilters';
import { useArticleReader } from '../../hooks/useArticleReader';
import { ArticleFilters } from './ArticleFilters';
import { ArticleList } from './ArticleList';
import { ArticlePreview } from './ArticlePreview';
import { ArticleResizer } from './ArticleResizer';
import { ReaderControls } from './ReaderControls';
import { LoadingIndicator } from '../../components/LoadingIndicator';
import { copyToClipboard } from '../../utils/clipboard';

export function ArticlesPage() {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const {
    filters,
    groupOptions,
    accountOptions,
    deferredSearch,
    updateFilters,
    resetFilters,
    selectArticle,
    loadArticles,
  } = useArticleFilters();
  const {
    config,
    updateConfig,
    readerControlsOpen,
    setReaderControlsOpen,
    readerControlsRef,
    readerToggleRef,
    previewRef,
    listCollapsed,
    setListCollapsed,
    isNarrowViewport,
    mobileReading,
    getSelectedArticleLink,
  } = useArticleReader();

  return (
    <section id="view-articles" className="view is-active">
      <div className={`article-layout${listCollapsed ? ' is-collapsed' : ''}${mobileReading ? ' is-mobile-reading' : ''}`}>
        <div className="panel article-list">
          <div className="panel-header article-sidebar-header">
            <div>
              <h2>{t('articles.title', 'Articles')}</h2>
              <p className="muted">{t('articles.librarySubtitle', 'Filter, scan, and open synced articles.')}</p>
            </div>
            <div className="toolbar article-sidebar-actions">
              <button
                className="btn ghost article-mobile-toggle"
                id="btn-article-filter-toggle"
                type="button"
                onClick={() => dispatch({ type: 'SET_FILTERS_COLLAPSED', collapsed: !state.filtersCollapsed })}
              >
                {state.filtersCollapsed ? t('articles.showFilters', 'Expand Filters') : t('articles.hideFilters', 'Collapse Filters')}
              </button>
              <button
                className="btn ghost article-reset-btn"
                id="btn-article-reset"
                type="button"
                onClick={() => { void resetFilters(); }}
              >
                {t('articles.resetFilters', 'Reset Filters')}
              </button>
            </div>
          </div>
          <ArticleFilters
            filters={filters}
            groupOptions={groupOptions}
            accountOptions={accountOptions}
            total={Number(state.lastFacetPayload?.total || 0)}
            onChange={updateFilters}
          />
          <LoadingIndicator isLoading={state.isArticleLoading} id="article-search-loading" />
          <ArticleList
            onSelect={selectArticle}
            onLoadMore={() => loadArticles({ ...filters, search: deferredSearch }, false)}
            onFilterByAccount={(biz) => updateFilters({ accountBiz: biz })}
          />
        </div>

        <ArticleResizer />

        <div className="panel article-preview">
          <div className="panel-header article-preview-topbar">
            <div>
              <h2>{t('articles.previewTitle', 'Article Preview')}</h2>
              <p className="muted">{t('articles.previewSubtitle', 'Review article details and adjust the reading layout.')}</p>
            </div>
            <div className="toolbar article-preview-toolbar">
              <button
                className="btn ghost article-mobile-back"
                id="btn-article-mobile-back"
                type="button"
                onClick={() => dispatch({ type: 'SET_MOBILE_READING', reading: false })}
              >
                {t('articles.backToList', 'Back to list')}
              </button>
              <button
                className="btn ghost article-toolbar-btn"
                id="btn-article-open-original"
                type="button"
                disabled={!getSelectedArticleLink()}
                onClick={() => { const link = getSelectedArticleLink(); if (link) window.open(link, '_blank', 'noopener,noreferrer'); }}
              >
                {t('articles.menu.openOriginal', 'Open original')}
              </button>
              <button
                className="btn ghost article-toolbar-btn"
                id="btn-article-copy-link"
                type="button"
                disabled={!getSelectedArticleLink()}
                onClick={async () => {
                  const link = getSelectedArticleLink();
                  if (!link) return;
                  await copyToClipboard(link);
                  showToast(t('articles.linkCopied', 'Link copied.'));
                }}
              >
                {t('articles.menu.copyLink', 'Copy link')}
              </button>
              <button className="icon-btn" id="reader-copy" type="button" aria-label="Copy article text" title="Copy article text" onClick={async () => {
                const content = previewRef.current?.querySelector('.reader');
                if (content) {
                  const text = (content as HTMLElement).innerText;
                  try { await copyToClipboard(text); showToast(t('articles.copied', '全文已复制')); } catch { showToast(t('articles.copyFailed', '复制失败')); }
                }
              }}>
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg></span>
              </button>
              <button
                className="icon-btn"
                id="reader-toggle"
                type="button"
                ref={readerToggleRef}
                aria-label="Typography settings"
                title="Typography settings"
                onClick={() => setReaderControlsOpen((prev) => !prev)}
              >
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v2H4zM4 11h16v2H4zM4 16h16v2H4z"/></svg></span>
              </button>
              <button
                className="icon-btn"
                id="btn-article-toggle"
                type="button"
                aria-label="Toggle list"
                title="Toggle list"
                onClick={() => {
                  if (isNarrowViewport && mobileReading) {
                    dispatch({ type: 'SET_MOBILE_READING', reading: false });
                    return;
                  }
                  setListCollapsed((prev) => !prev);
                }}
              >
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v2H4zM4 11h10v2H4zM4 16h16v2H4z"/></svg></span>
              </button>
            </div>
          </div>
          <ReaderControls
            isOpen={readerControlsOpen}
            controlsRef={readerControlsRef}
            config={config}
            updateConfig={updateConfig}
          />
          <ArticlePreview previewRef={previewRef} />
        </div>
      </div>
    </section>
  );
}
