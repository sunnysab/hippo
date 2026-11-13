import { useRef, useState } from 'react';
import { ContextMenu } from '../../components/ContextMenu';
import { useI18n } from '../../i18n';
import { getSourceLabel, getSourceTone } from '../../utils/source';
import type { ArticleSource } from '../../hooks/useArticleSources';
import type { ArticleFiltersState, ArticleSelectOption } from './filtering';

interface ViewRailProps {
  filters: ArticleFiltersState;
  sources: ArticleSource[];
  groupOptions: ArticleSelectOption[];
  onChange: (patch: Partial<ArticleFiltersState>) => void;
  onChooseCaixinChannels: () => void;
}

/**
 * The left column: what the reader is looking at, not where they can go.
 *
 * Global navigation stays in the top bar; this is the article list's own scope, so
 * clicking a source or a group narrows the list in place. It only appears when
 * there is room for it (see `.article-layout.with-rail`), which is also why the
 * view switch stays in the list and sources move to the workspace toolbar.
 */
export function ViewRail({ filters, sources, groupOptions, onChange, onChooseCaixinChannels }: ViewRailProps) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const menuTrigger = useRef<HTMLButtonElement | null>(null);
  const openMenu = (button: HTMLButtonElement, x: number, y: number) => {
    menuTrigger.current = button;
    setMenu({ x: Math.max(8, Math.min(x, window.innerWidth - 200)), y: Math.max(8, Math.min(y, window.innerHeight - 60)) });
  };
  // The number is why these rows exist, so it is part of the name a screen
  // reader announces and not only a span on the right.
  const subscriptionCount = (count: number) => (
    t('articles.railSubscriptions', '{count} subscriptions').replace('{count}', String(count))
  );

  const view = filters.watchLater ? 'watch-later' : filters.starred ? 'starred' : filters.read === 'unread' ? 'unread' : 'all';
  const views = [
    { key: 'all', label: t('articles.viewAll', '全部'), patch: { read: '', starred: '' } },
    { key: 'unread', label: t('articles.viewUnread', '未读'), patch: { read: 'unread', starred: '' } },
    { key: 'starred', label: t('articles.viewStarred', '收藏'), patch: { read: '', starred: 'true' } },
    { key: 'watch-later', label: t('articles.watchLater', 'Watch Later'), patch: { watchLater: true } },
  ];

  return (
    <aside className="article-view-rail" aria-label={t('articles.rail', '文章视图')}>
      <div className="view-rail-section">
        {views.map((item) => (
          <button
            key={item.key}
            className={`view-rail-item${view === item.key ? ' is-active' : ''}`}
            id={`rail-view-${item.key}`}
            type="button"
            aria-pressed={view === item.key}
            onClick={() => onChange(item.patch)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {!filters.watchLater && sources.length > 0 ? (
        <div className="view-rail-section">
          <h3 className="view-rail-title">{t('articles.railSources', '来源')}</h3>
          <button
            className={`view-rail-item${filters.source ? '' : ' is-active'}`}
            id="rail-source-all"
            type="button"
            aria-pressed={!filters.source}
            onClick={() => onChange({ source: '' })}
          >
            {t('articles.railAllSources', '全部来源')}
          </button>
          {sources.map((item) => (
            <button
              key={item.source}
              className={`view-rail-item${filters.source === item.source ? ' is-active' : ''}`}
              type="button"
              aria-pressed={filters.source === item.source}
              aria-label={`${getSourceLabel(item.source, t)} · ${subscriptionCount(item.channels)}`}
              aria-haspopup={item.source === 'caixin' && item.discoverable ? 'menu' : undefined}
              onContextMenu={(event) => {
                if (item.source !== 'caixin' || !item.discoverable) return;
                event.preventDefault();
                openMenu(event.currentTarget, event.clientX, event.clientY);
              }}
              onKeyDown={(event) => {
                if (item.source !== 'caixin' || !item.discoverable || !(event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10'))) return;
                event.preventDefault();
                const rect = event.currentTarget.getBoundingClientRect();
                openMenu(event.currentTarget, rect.left, rect.bottom);
              }}
              onClick={() => onChange({ source: item.source })}
            >
              <span className={`view-rail-dot item-show-type-${getSourceTone(item.source)}`} aria-hidden="true" />
              <span className="view-rail-label">{getSourceLabel(item.source, t)}</span>
              <span className="view-rail-count" aria-hidden="true">{item.channels}</span>
            </button>
          ))}
        </div>
      ) : null}

      {!filters.watchLater && groupOptions.length > 0 ? (
        <div className="view-rail-section">
          <h3 className="view-rail-title">{t('articles.railGroups', '分组')}</h3>
          <button
            className={`view-rail-item${filters.groupId ? '' : ' is-active'}`}
            id="rail-group-all"
            type="button"
            aria-pressed={!filters.groupId}
            onClick={() => onChange({ groupId: '' })}
          >
            {t('articles.railAllGroups', '全部分组')}
          </button>
          {groupOptions.map((group) => (
            <button
              key={group.value}
              className={`view-rail-item${filters.groupId === group.value ? ' is-active' : ''}`}
              type="button"
              aria-pressed={filters.groupId === group.value}
              aria-label={`${group.label} · ${subscriptionCount(group.count ?? 0)}`}
              onClick={() => onChange({ groupId: group.value })}
            >
              <span className="view-rail-label">{group.label}</span>
              <span className="view-rail-count" aria-hidden="true">{group.count ?? 0}</span>
            </button>
          ))}
        </div>
      ) : null}
      <ContextMenu id="caixin-source-menu" isOpen={Boolean(menu)} x={menu?.x || 0} y={menu?.y || 0} onClose={() => setMenu(null)}>
        <div role="menu" aria-label={t('articles.chooseCaixinChannels', '选择财新板块')}>
          <button className="context-item" type="button" role="menuitem" autoFocus
            onKeyDown={(event) => {
              if (event.key !== 'Escape') return;
              event.stopPropagation();
              setMenu(null);
              menuTrigger.current?.focus();
            }}
            onClick={() => { setMenu(null); onChooseCaixinChannels(); }}>
            {t('articles.chooseCaixinChannels', '选择财新板块')}
          </button>
        </div>
      </ContextMenu>
    </aside>
  );
}
