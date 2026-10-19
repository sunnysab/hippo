import { useEffect, useRef, useState, type RefObject } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useArticlesState, type ArticlePayload } from '../../store/articles';
import { formatDuration, timestampHref } from '../../utils/duration';
import {
  isOutlineBlock, jumpToOutline, outlineBlockSelector, sameContentVersion,
  type ArticleOutline, type OutlineItem,
} from './outline';

interface TableOfContentsProps {
  items: OutlineItem[];
  payload: ArticlePayload;
  containerRef: RefObject<HTMLElement | null>;
  onClose: () => void;
}

interface NavigationItem extends OutlineItem {
  selector: string;
  time?: number;
  href?: string;
}

interface NavigationSection {
  item: NavigationItem;
  children: NavigationItem[];
}

function originalSections(items: OutlineItem[]): NavigationSection[] {
  const sections: NavigationSection[] = [];
  const base = Math.min(...items.map((item) => item.level));
  for (const item of items) {
    const next = { ...item, selector: `#${item.id}` };
    if (!sections.length || item.level <= base) sections.push({ item: next, children: [] });
    else sections[sections.length - 1].children.push(next);
  }
  return sections;
}

function NavigationList({ sections, containerRef }: {
  sections: NavigationSection[];
  containerRef: RefObject<HTMLElement | null>;
}) {
  const { t } = useI18n();
  const [activeId, setActiveId] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const items = sections.flatMap((section) => [section.item, ...section.children]);
    let frame = 0;
    const update = () => {
      frame = 0;
      const scrolls = /auto|scroll/.test(getComputedStyle(container).overflowY);
      const top = scrolls ? container.getBoundingClientRect().top + 72 : 128;
      let current = items[0]?.id || '';
      for (const item of items) {
        const target = container.querySelector(item.selector);
        if (target && target.getBoundingClientRect().top <= top) current = item.id;
      }
      setActiveId(current);
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    update();
    container.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    return () => {
      cancelAnimationFrame(frame);
      container.removeEventListener('scroll', schedule);
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
    };
  }, [sections, containerRef]);

  const jump = (item: NavigationItem, play = false) => {
    const container = containerRef.current;
    const target = container?.querySelector<HTMLElement>(item.selector);
    if (!container || !target) return;
    if (play) target.querySelector<HTMLElement>('.reader-anchor.is-action')?.click();
    else {
      jumpToOutline(container, target);
      setActiveId(item.id);
    }
  };

  const label = (item: NavigationItem, active: boolean) => (
    <>
      <button
        className={`reader-toc-item${active ? ' is-active' : ''}`}
        type='button'
        aria-current={active ? 'location' : undefined}
        title={item.text}
        onClick={() => jump(item)}
      >{item.text}</button>
      {typeof item.time === 'number' && Number.isFinite(item.time) && item.time >= 0 ? (
        item.href ? (
          <a className='reader-toc-time' href={item.href} target='_blank' rel='noreferrer'
            aria-label={`${t('articles.outline.playAt', 'Play from')} ${formatDuration(item.time)}`}>
            {formatDuration(item.time)}
          </a>
        ) : (
          <button className='reader-toc-time' type='button' onClick={() => jump(item, true)}
            aria-label={`${t('articles.outline.playAt', 'Play from')} ${formatDuration(item.time)}`}>
            {formatDuration(item.time)}
          </button>
        )
      ) : null}
    </>
  );

  return (
    <nav className='reader-toc-list' aria-label={t('articles.outline.navigation', 'Article navigation')}>
      {sections.map(({ item, children }, index) => {
        const hidden = collapsed.has(item.id);
        const active = activeId === item.id || (hidden && children.some((child) => child.id === activeId));
        return (
          <div className='reader-toc-section' key={item.id}>
            <div className={`reader-toc-row${active ? ' is-active' : ''}`}>
              {children.length ? (
                <button className='reader-toc-fold' type='button' aria-expanded={!hidden}
                  aria-controls={`outline-children-${item.id}`}
                  aria-label={`${hidden ? t('articles.outline.expand', 'Expand') : t('articles.outline.collapse', 'Collapse')} ${item.text}`}
                  onClick={() => setCollapsed((current) => {
                    const next = new Set(current);
                    if (next.has(item.id)) next.delete(item.id); else next.add(item.id);
                    return next;
                  })}>
                  <svg viewBox='0 0 16 16' aria-hidden='true'><path d='m6 4 4 4-4 4' /></svg>
                </button>
              ) : <span className='reader-toc-number' aria-hidden='true'>{String(index + 1).padStart(2, '0')}</span>}
              {label(item, active)}
            </div>
            {children.length ? (
              <div id={`outline-children-${item.id}`} className='reader-toc-children' hidden={hidden}>
                {children.map((child) => (
                  <div className={`reader-toc-row${activeId === child.id ? ' is-active' : ''}`} key={child.id}>
                    {label(child, activeId === child.id)}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
    </nav>
  );
}

export function TableOfContents({ items, payload, containerRef, onClose }: TableOfContentsProps) {
  const { t } = useI18n();
  const { dispatch } = useArticlesState();
  const [mode, setMode] = useState<'original' | 'ai'>(items.length ? 'original' : 'ai');
  const [result, setResult] = useState<ArticleOutline | null>(null);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [pollRun, setPollRun] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const closeRef = useRef(onClose);
  useEffect(() => { closeRef.current = onClose; }, [onClose]);
  const path = `/api/article/${payload.article.id}/outline`;

  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const response = await apiGet(path, { signal: abort.signal }) as unknown as ArticleOutline;
        if (abort.signal.aborted) return;
        setResult(response);
        setError('');
        if (response.state === 'queued' || response.state === 'processing') timer = setTimeout(() => { void load(); }, 2500);
      } catch (cause) {
        if (!abort.signal.aborted && !isAuthError(cause)) setError(t('articles.outline.loadFailed', 'Could not load the outline. Reopen this panel to retry.'));
      } finally {
        if (!abort.signal.aborted) setLoading(false);
      }
    };
    void load();
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        closeRef.current();
        document.getElementById('btn-article-toc-toggle')?.focus();
      }
    };
    document.addEventListener('keydown', escape);
    return () => { abort.abort(); clearTimeout(timer); document.removeEventListener('keydown', escape); };
  }, [path, pollRun, t]);

  const generate = async () => {
    setSending(true);
    setError('');
    try {
      const response = await apiSend(path, 'POST', {
        force: result?.state === 'ready', content_updated_at: payload.content_updated_at,
      }, { signal: controller.current?.signal }) as unknown as ArticleOutline;
      if (controller.current?.signal.aborted) return;
      setResult(response);
      setPollRun((value) => value + 1);
    } catch (cause) {
      if (!controller.current?.signal.aborted && !isAuthError(cause)) {
        setError(cause instanceof Error ? cause.message : t('articles.outline.failed', 'Outline generation failed. Please retry.'));
      }
    } finally { if (!controller.current?.signal.aborted) setSending(false); }
  };

  const mismatch = result && !sameContentVersion(result.content_updated_at, payload.content_updated_at);
  const busy = sending || result?.state === 'queued' || result?.state === 'processing';
  const textBlocks = (payload.content || []).filter(isOutlineBlock);
  const canSeek = (payload.content || []).some((block) => (
    (block.type === 'video' && /^\/api\/media\/[1-9]\d*\/video$/.test(block.url || ''))
    || (block.type === 'audio' && Boolean(block.url))
  ));
  const originalHref = payload.article.source_url || payload.article.link || '';
  const sections = mode === 'original' ? originalSections(items) : result?.state === 'ready' && !mismatch
    ? result.sections.map((section) => {
      const item = (topic: { title: string; start_block: number }, level: number): NavigationItem => {
        const block = textBlocks[topic.start_block];
        const time = block && ['paragraph', 'text', 'link'].includes(block.type) ? block.t : undefined;
        return {
          id: `ai-${level}-${topic.start_block}`, level, text: topic.title,
          selector: outlineBlockSelector(topic.start_block),
          time: canSeek || originalHref ? time : undefined,
          href: !canSeek && originalHref && time !== undefined ? timestampHref(originalHref, time) || originalHref : undefined,
        };
      };
      return { item: item(section, 2), children: section.children.map((child) => item(child, 3)) };
    }) : [];

  const reload = async () => {
    try {
      const next = await apiGet(`/api/article/${payload.article.id}`, { signal: controller.current?.signal });
      if (!controller.current?.signal.aborted) dispatch({ type: 'SELECT_ARTICLE', id: payload.article.id, payload: next as unknown as ArticlePayload });
    } catch (cause) {
      if (!controller.current?.signal.aborted && !isAuthError(cause)) setError(t('articles.outline.loadFailed', 'Could not reload the article.'));
    }
  };

  return (
    <aside className='reader-toc' id='article-outline-panel' aria-label={t('articles.toc', 'Contents')}>
      <header className='reader-toc-header'>
        <div><span className='reader-toc-eyebrow'>{t('articles.outline.eyebrow', 'READING MAP')}</span>
          <h2 className='reader-toc-title'>{t('articles.outline.title', 'Content outline')}</h2></div>
        <button className='icon-btn' type='button' onClick={() => { onClose(); document.getElementById('btn-article-toc-toggle')?.focus(); }} aria-label={t('articles.outline.close', 'Hide outline')}>
          <svg viewBox='0 0 20 20' width='18' height='18' fill='none' stroke='currentColor' strokeWidth='1.5' aria-hidden='true'><path d='m5 5 10 10M15 5 5 15' /></svg>
        </button>
      </header>
      <div className='reader-toc-switch' role='group' aria-label={t('articles.outline.mode', 'Outline source')}>
        <button type='button' aria-pressed={mode === 'original'} onClick={() => setMode('original')}>{t('articles.outline.original', 'Original headings')}</button>
        <button type='button' aria-pressed={mode === 'ai'} onClick={() => setMode('ai')}>{t('articles.outline.ai', 'AI outline')}<span aria-hidden='true'>✦</span></button>
      </div>
      <div className='reader-toc-body'>
        {mode === 'ai' ? <p className='reader-toc-note'>{t('articles.outline.hint', 'In reading order. Titles jump to text; timestamps play media.')}</p> : null}
        {mode === 'ai' && (loading || busy) ? (
          <div className='reader-toc-status' role='status'><span className='reader-toc-pulse' />{loading ? t('articles.outline.loading', 'Loading outline…') : t('articles.outline.working', 'Organizing the full text… You can keep reading.')}</div>
        ) : null}
        {mode === 'ai' && (mismatch || result?.state === 'stale') ? (
          <div className='reader-toc-status'>
            <p>{t('articles.outline.stale', 'The text has changed. Generate a new outline to keep links accurate.')}</p>
            {mismatch ? <button className='btn ghost' type='button' onClick={() => { void reload(); }}>{t('articles.outline.reload', 'Reload article')}</button> : null}
          </div>
        ) : null}
        {mode === 'ai' && (error || result?.state === 'failed') ? (
          <p className='reader-toc-error' role='alert'>{error || t(result?.error_code === 'outline.content_changed' ? 'articles.outline.stale' : 'articles.outline.failed', 'Outline generation failed. Please retry.')}</p>
        ) : null}
        {sections.length ? <NavigationList key={mode} sections={sections} containerRef={containerRef} /> : (
          !busy && !loading ? <div className='reader-toc-empty'>
            <svg viewBox='0 0 32 32' width='32' height='32' fill='none' stroke='currentColor' strokeWidth='1.2' aria-hidden='true'><path d='M8 7h16M8 16h11M8 25h16M4 7h.1M4 16h.1M4 25h.1' /></svg>
            <p>{mode === 'original' ? t('articles.outline.noHeadings', 'No headings in the original text.') : textBlocks.length ? t('articles.outline.empty', 'Turn a long read into a clear path.') : t('articles.outline.noText', 'No readable text yet. Sync or finish transcription first.')}</p>
            {mode === 'original' ? <button className='btn ghost' type='button' onClick={() => setMode('ai')}>{t('articles.outline.tryAi', 'Use AI outline')}</button> : null}
          </div> : null
        )}
      </div>
      {mode === 'ai' ? (
        <footer className='reader-toc-footer'>
          <button className='btn' type='button' disabled={loading || busy || !textBlocks.length || Boolean(mismatch)} onClick={() => { void generate(); }}>
            {busy ? t('articles.outline.generating', 'Generating…') : result?.state === 'ready' ? t('articles.outline.regenerate', 'Regenerate outline') : t('articles.outline.generate', 'Generate outline')}
          </button>
          <span>{t('articles.outline.disclaimer', 'AI generated · Original text stays unchanged')}</span>
        </footer>
      ) : null}
    </aside>
  );
}
