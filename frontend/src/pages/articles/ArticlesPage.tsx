import { useEffect, useState } from 'react';
import '../../styles/watch-later.css';
import { useWatchLater } from '../../hooks/useWatchLater';
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
import { useArticleSources } from '../../hooks/useArticleSources';
import { getSourceLabel } from '../../utils/source';
import { AddSubscriptionModal } from '../groups/AddSubscriptionModal';

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
    cancelArticleLoad,
    loadArticles,
    listError,
  } = useArticleFilters();
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
  const readingActive = !isNarrowViewport || mobileReading;
  const [autoPlayId, setAutoPlayId] = useState<number | null>(null);
  const [endedArticleId, setEndedArticleId] = useState<number | null>(null);
  const [showSubscribe, setShowSubscribe] = useState(false);
  if (endedArticleId !== null && (!filters.watchLater || !readingActive || state.selectedArticleId !== endedArticleId)) {
    setEndedArticleId(null);
  }
  if (autoPlayId !== null && (!filters.watchLater || !readingActive || state.selectedArticleId !== autoPlayId)) {
    setAutoPlayId(null);
  }
  const watchLater = useWatchLater(async () => {
    if (filters.watchLater) await loadArticles(filters, true);
  });
  const queueIndex = filters.watchLater ? state.articles.findIndex((article) => article.id === state.selectedArticleId) : -1;
  const openQueueItem = async (index: number) => {
    const article = state.articles[index];
    if (!article || watchLater.busy || state.isArticleLoading) return;
    setAutoPlayId(null);
    setEndedArticleId(null);
    await selectArticle(article.id);
  };
  useEffect(() => {
    if (endedArticleId === null || !filters.watchLater || !readingActive || state.selectedArticleId !== endedArticleId
      || watchLater.busy || state.isArticleLoading) return;
    let cancelled = false;
    // Wait for the latest saved order; cleanup cancels work after navigation.
    queueMicrotask(() => {
      if (cancelled) return;
      setEndedArticleId(null);
      const index = state.articles.findIndex((article) => article.id === endedArticleId);
      if (listError || index < 0) {
        setAutoPlayId(null);
        showToast(t('articles.watchLaterPlaybackFailed', 'Playback could not continue. Press play or choose another item.'));
        return;
      }
      const next = state.articles[index + 1];
      setAutoPlayId(next?.id ?? null);
      if (next) void selectArticle(next.id);
    });
    return () => { cancelled = true; };
  }, [endedArticleId, filters.watchLater, readingActive, state.selectedArticleId, watchLater.busy, state.isArticleLoading, state.articles, listError, selectArticle, showToast, t]);
  // Keep the source selector available whenever the wide navigation is hidden.
  const showViewRail = useMediaQuery('(min-width: 1600px)');
  const { sources, error: sourcesFailed, reload: reloadSources } = useArticleSources();
  const railVisible = showViewRail && !listCollapsed;
  const listVisible = isNarrowViewport ? !mobileReading : !listCollapsed;
  const listLabel = isNarrowViewport
    ? t('articles.backToList', 'Back to list')
    : listCollapsed ? t('articles.expandList', 'Show list') : t('articles.collapseList', 'Hide list');
  const filterCount = [filters.groupId, filters.accountBiz, filters.itemShowType, filters.search.trim(), filters.read, filters.starred].filter(Boolean).length;
  const revealList = () => {
    setEndedArticleId(null);
    setAutoPlayId(null);
    cancelArticleLoad();
    setListCollapsed(false);
    dispatch({ type: 'SET_MOBILE_READING', reading: false });
  };
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
      <div className="toolbar article-workspace-toolbar" role="toolbar" aria-label={t('articles.navigation', 'Article navigation')}>
        {!isNarrowViewport || mobileReading ? (
          <button className="btn ghost article-list-toggle" id="btn-article-toggle" type="button"
            aria-label={listLabel} title={listLabel} aria-expanded={listVisible} aria-controls="article-list-panel"
            onClick={() => { if (isNarrowViewport) revealList(); else setListCollapsed((current) => !current); }}>
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
              {isNarrowViewport ? <path d="m10 5-7 7 7 7M3 12h18" /> : <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></>}
            </svg>
            <span>{listLabel}</span>
          </button>
        ) : null}
        {!filters.watchLater && (!railVisible || sourcesFailed || sources.length === 0) ? (
          <div className="article-source-control">
            <select id="article-source-filter" aria-label={t('articles.railSources', 'Source')} value={filters.source}
              onChange={(event) => { updateFilters({ source: event.target.value }); revealList(); }}>
              <option value="">{t('articles.railAllSources', 'All sources')}</option>
              {filters.source && !sources.some((item) => item.source === filters.source) ? (
                <option value={filters.source}>{getSourceLabel(filters.source, t)}</option>
              ) : null}
              {sources.map((item) => <option key={item.source} value={item.source}>{getSourceLabel(item.source, t)}</option>)}
            </select>
            {sourcesFailed ? <button className="btn ghost" type="button" onClick={() => { void reloadSources(); }}>
              {t('articles.retrySources', 'Retry sources')}
            </button> : null}
          </div>
        ) : null}
        {!filters.watchLater ? <button className={`btn ghost article-filter-toggle${state.filtersCollapsed ? '' : ' is-active'}`}
          id="btn-article-filter-toggle" type="button" aria-expanded={listVisible && !state.filtersCollapsed}
          aria-controls="article-filter-drawer" onClick={() => {
            revealList();
            dispatch({ type: 'SET_FILTERS_COLLAPSED', collapsed: listVisible ? !state.filtersCollapsed : false });
          }}>
          {t('articles.toggleFilters', 'Filters')}{filterCount ? ` · ${filterCount}` : ''}
        </button> : null}
      </div>
      <div
        className={`article-layout${listCollapsed ? ' is-collapsed' : ''}${mobileReading ? ' is-mobile-reading' : ''}${
          railVisible ? ' with-rail' : ''
        }`}
      >
        {railVisible ? (
          <ViewRail sources={sources} filters={filters} groupOptions={groupOptions} onChange={updateFilters}
            onChooseCaixinChannels={() => setShowSubscribe(true)} />
        ) : null}
        <div className="panel article-list" id="article-list-panel">
          <div className="panel-header article-sidebar-header">
            <div>
              <h2>{filters.watchLater ? t('articles.watchLater', 'Watch Later') : t('articles.title', 'Articles')}</h2>
              {filters.watchLater ? <span className="meta-note">{state.articles.length} {t('articles.watchLaterItems', 'items')}</span> : <ArticleFilterSummary
                filters={filters}
                groupOptions={groupOptions}
                accountOptions={accountOptions}
                total={Number(state.lastFacetPayload?.total || 0)}
              />}
            </div>
            {!filters.watchLater ? <div className="toolbar article-sidebar-actions">
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
            </div> : null}
          </div>
          <ArticleFilters
            filters={filters}
            groupOptions={groupOptions}
            accountOptions={accountOptions}
            onChange={updateFilters}
            onReset={() => { void resetFilters(); }}
          />
          <LoadingIndicator isLoading={state.isArticleLoading} id="article-search-loading" />
          {listError ? <div className="watch-later-error" role="alert">
            <p>{listError}</p>
            <button type="button" className="btn ghost" onClick={() => { void loadArticles(filters, true); }}>
              {t('common.retry', 'Retry')}
            </button>
          </div> : state.isArticleLoading && state.articles.length === 0 ? null : <ArticleList
            onSelect={async (id) => { setAutoPlayId(null); setEndedArticleId(null); await selectArticle(id); }}
            watchLater={filters.watchLater}
            watchLaterBusy={watchLater.busy}
            onToggleWatchLater={(article) => { void watchLater.toggle(article); }}
            onMoveWatchLater={(article, direction) => { void watchLater.move(article, direction); }}
            onLoadMore={() => loadArticles({ ...filters, search: deferredSearch }, false)}
            onFilterByAccount={(biz) => updateFilters({ accountBiz: biz })}
          />}
        </div>

        <ArticleResizer />

        <div className="panel article-preview">
          <div className="panel-header article-preview-topbar">
            <div className="toolbar article-preview-toolbar">
              {filters.watchLater ? <div className="watch-later-navigation" role="group" aria-label={t('articles.watchLater', 'Watch Later')}>
                <button type="button" className="btn ghost" disabled={queueIndex <= 0 || watchLater.busy || state.isArticleLoading || !state.currentArticlePayload}
                  onClick={() => { void openQueueItem(queueIndex - 1); }}>{t('articles.watchLaterPrevious', 'Previous item')}</button>
                <span className="meta-note" aria-live="polite">{queueIndex < 0 ? '—' : queueIndex + 1} / {state.articles.length}</span>
                <button type="button" className="btn ghost" disabled={queueIndex < 0 || queueIndex >= state.articles.length - 1 || watchLater.busy || state.isArticleLoading || !state.currentArticlePayload}
                  onClick={() => { void openQueueItem(queueIndex + 1); }}>{t('articles.watchLaterNext', 'Next item')}</button>
              </div> : null}
              <button type="button" className="btn ghost article-toolbar-btn" disabled={!state.currentArticlePayload || watchLater.busy}
                aria-pressed={Boolean(state.currentArticlePayload?.article.watch_later_at)}
                onClick={() => { if (state.currentArticlePayload) void watchLater.toggle(state.currentArticlePayload.article); }}>
                {state.currentArticlePayload?.article.watch_later_at ? t('articles.removeWatchLater', 'Remove from Watch Later') : t('articles.addWatchLater', 'Add to Watch Later')}
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

            </div>
          </div>
          <ArticlePreview
            autoPlay={Boolean(filters.watchLater && readingActive && isMediaArticle && autoPlayId === state.selectedArticleId)}
            onMediaEnded={(id) => {
              if (!filters.watchLater || !readingActive || !isMediaArticle || id !== state.selectedArticleId) return;
              setEndedArticleId(id);
            }}
            onMediaError={(id) => {
              if (!readingActive || id !== state.selectedArticleId) return;
              setEndedArticleId(null);
              setAutoPlayId(null);
              showToast(t('articles.watchLaterPlaybackFailed', 'Playback could not continue. Press play or choose another item.'));
            }}
            previewRef={previewRef}
            sidePanel={sidePanel}
            onSidePanelToggle={(panel) => setSidePanel((current) => (current === panel ? '' : panel))}
            onOpenEvent={(eventId) => {
              // Switching to the story clears the direction filter: the two are
              // different questions and combining them usually empties the list.
              updateFilters({ eventId: String(eventId), topic: '' });
              revealList();
            }}
          />
        </div>
      </div>
      {showSubscribe ? (
        <AddSubscriptionModal
          initialSource="caixin"
          groupId={null}
          onClose={() => setShowSubscribe(false)}
          onSubscribed={() => { void reloadSources(); }}
        />
      ) : null}
    </section>
  );
}
