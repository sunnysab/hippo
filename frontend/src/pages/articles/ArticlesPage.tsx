import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiSend } from '../../api';
import { useArticleFilters } from '../../hooks/useArticleFilters';
import { useArticleReader } from '../../hooks/useArticleReader';
import { ArticleFilters } from './ArticleFilters';
import { ArticleFilterSummary } from './ArticleFilterSummary';
import { ArticleList } from './ArticleList';
import { ArticlePreview } from './ArticlePreview';
import { ArticleResizer } from './ArticleResizer';
import { ViewRail } from './ViewRail';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import { ReaderControls } from './ReaderControls';
import { Popover } from '../../components/Popover';
import { LoadingIndicator } from '../../components/LoadingIndicator';
import { copyToClipboard } from '../../utils/clipboard';
import { articleToMarkdown } from './exportMarkdown';
import { useSyncRun } from '../../hooks/useSyncRun';
import { getSyncRunLabel } from '../../utils/sync';

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
  // 宽屏才给左栏：窄屏上它会把正文挤到没法读，那里继续用列表头部的控件。
  const showViewRail = useMediaQuery('(min-width: 1600px)');
  const {
    config,
    updateConfig,
    sidePanel,
    setSidePanel,
    previewRef,
    listCollapsed,
    setListCollapsed,
    isNarrowViewport,
    mobileReading,
    getSelectedArticleLink,
  } = useArticleReader();
  const { runSync, task: syncTask, running: isSyncing } = useSyncRun();
  // Media entries are the ones with a transcription queue behind them; the button
  // is gated on the kind rather than the source, so a new source needs no change.
  const isMediaArticle = ['video', 'audio'].includes(
    String(state.currentArticlePayload?.article.content_kind || ''),
  );

  const transcribeArticle = async (forceAsr: boolean) => {
    const article = state.currentArticlePayload?.article;
    if (!article) return;
    try {
      await apiSend(`/api/article/${article.id}/transcribe${forceAsr ? '?force_asr=true' : ''}`, 'POST', {});
      showToast(t('articles.transcribeQueued', '已加入转写队列。'));
    } catch (error) {
      showToast((error as Error)?.message || t('articles.transcribeFailed', '加入队列失败。'));
    }
  };

  return (
    <section id="view-articles" className="view is-active">
      <div
        className={`article-layout${listCollapsed ? ' is-collapsed' : ''}${mobileReading ? ' is-mobile-reading' : ''}${
          showViewRail && !listCollapsed ? ' with-rail' : ''
        }`}
      >
        {showViewRail && !listCollapsed ? (
          <ViewRail filters={filters} groupOptions={groupOptions} onChange={updateFilters} />
        ) : null}
        <div className="panel article-list">
          <div className="panel-header article-sidebar-header">
            <div>
              <h2>{t('articles.title', 'Articles')}</h2>
              <ArticleFilterSummary
                filters={filters}
                groupOptions={groupOptions}
                accountOptions={accountOptions}
                total={Number(state.lastFacetPayload?.total || 0)}
              />
            </div>
            <div className="toolbar article-sidebar-actions">
              {/* The scope is whatever the list is filtered to: one source, one
                  group, one account, or every subscription the reader has. All
                  of them go out together and the backend intersects them. */}
              <button
                className="btn ghost"
                id="btn-articles-sync"
                type="button"
                disabled={isSyncing}
                title={t('articles.updateNowHint', 'Update the current scope')}
                onClick={() => {
                  void runSync({
                    source: filters.source,
                    groupId: filters.groupId,
                    bizList: filters.accountBiz ? [filters.accountBiz] : undefined,
                  });
                }}
              >
                {t('articles.updateNow', 'Update now')}
              </button>
              {syncTask ? (
                <span className="meta-note" id="article-sync-status" role="status">
                  {getSyncRunLabel(syncTask.status, t)}
                </span>
              ) : null}
              <button
                className={`btn ghost article-filter-toggle${state.filtersCollapsed ? '' : ' is-active'}`}
                id="btn-article-filter-toggle"
                type="button"
                aria-expanded={!state.filtersCollapsed}
                aria-controls="article-filter-drawer"
                onClick={() => dispatch({ type: 'SET_FILTERS_COLLAPSED', collapsed: !state.filtersCollapsed })}
              >
                {t('articles.toggleFilters', 'Filters')}
              </button>
            </div>
          </div>
          <ArticleFilters
            filters={filters}
            groupOptions={groupOptions}
            accountOptions={accountOptions}
            onChange={updateFilters}
            onReset={() => { void resetFilters(); }}
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
              <Popover
                id="article-copy-menu"
                label={t('articles.copy', '复制')}
                triggerClassName="icon-btn"
                trigger={<span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg></span>}
              >
                {(close) => (
                  <>
                    <button
                      className="context-item"
                      id="article-copy-plain"
                      type="button"
                      disabled={!state.currentArticlePayload}
                      onClick={async () => {
                        const content = previewRef.current?.querySelector('.reader');
                        if (content) {
                          try { await copyToClipboard((content as HTMLElement).innerText); showToast(t('articles.copied', '全文已复制')); } catch { showToast(t('articles.copyFailed', '复制失败')); }
                        }
                        close();
                      }}
                    >
                      {t('articles.copyPlain', '纯文本')}
                    </button>
                    <button
                      className="context-item"
                      id="article-copy-markdown"
                      type="button"
                      disabled={!state.currentArticlePayload}
                      onClick={async () => {
                        const payload = state.currentArticlePayload;
                        if (payload) {
                          try { await copyToClipboard(articleToMarkdown(payload)); showToast(t('articles.copied', '全文已复制')); } catch { showToast(t('articles.copyFailed', '复制失败')); }
                        }
                        close();
                      }}
                    >
                      {t('articles.copyMarkdown', 'Markdown')}
                    </button>
                  </>
                )}
              </Popover>
              <Popover
                id="reader-controls"
                label={t('articles.typography', 'Typography settings')}
                triggerClassName="icon-btn is-text"
                trigger={<span aria-hidden="true">Aa</span>}
              >
                <ReaderControls config={config} updateConfig={updateConfig} />
              </Popover>
              {isMediaArticle ? (
                <Popover
                  id='article-transcribe-menu'
                  label={t('articles.transcribe', '转写')}
                  triggerClassName='btn ghost article-toolbar-btn'
                  trigger={t('articles.transcribe', '转写')}
                >
                  {(close) => (
                    <>
                      <button
                        className='context-item'
                        id='article-transcribe'
                        type='button'
                        onClick={async () => { await transcribeArticle(false); close(); }}
                      >
                        {t('articles.transcribeNow', '转写这篇')}
                      </button>
                      <button
                        className='context-item'
                        id='article-transcribe-force'
                        type='button'
                        title={t('articles.transcribeForceHint', '忽略现成的字幕或转录稿，改用 ASR 重转')}
                        onClick={async () => { await transcribeArticle(true); close(); }}
                      >
                        {t('articles.transcribeForce', '跳过字幕，用 ASR 重转')}
                      </button>
                    </>
                  )}
                </Popover>
              ) : null}
              <button
                className={`btn ghost article-toolbar-btn${sidePanel === 'toc' ? ' is-active' : ''}`}
                id="btn-article-toc-toggle"
                type="button"
                disabled={!state.currentArticlePayload}
                aria-pressed={sidePanel === 'toc'}
                aria-expanded={sidePanel === 'toc'}
                aria-controls='article-outline-panel'
                title={t('articles.toc', 'Contents')}
                onClick={() => setSidePanel((current) => (current === 'toc' ? '' : 'toc'))}
              >
                {t('articles.toc', '目录')}
              </button>
              <button
                className="btn article-toolbar-btn"
                id="btn-article-ai-toggle"
                type="button"
                disabled={!state.currentArticlePayload}
                aria-pressed={sidePanel === 'ai'}
                title={t('articles.ai.open', '解读')}
                onClick={() => setSidePanel((current) => (current === 'ai' ? '' : 'ai'))}
              >
                {t('articles.ai.open', '解读')}
              </button>
              <button
                className="icon-btn"
                id="btn-article-toggle"
                type="button"
                aria-label={t('articles.toggleList', 'Toggle list')}
                title={t('articles.toggleList', 'Toggle list')}
                onClick={() => {
                  if (isNarrowViewport && mobileReading) {
                    dispatch({ type: 'SET_MOBILE_READING', reading: false });
                    return;
                  }
                  setListCollapsed((prev) => !prev);
                }}
              >
                <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path fillRule="evenodd" d="M3 5h18v14H3zM9 5v14H6V5z"/></svg></span>
              </button>
            </div>
          </div>
          <ArticlePreview
            previewRef={previewRef}
            sidePanel={sidePanel}
            onSidePanelToggle={(panel) => setSidePanel((current) => (current === panel ? '' : panel))}
          />
        </div>
      </div>
    </section>
  );
}
