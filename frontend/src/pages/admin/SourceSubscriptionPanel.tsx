import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiGet } from '../../api';
import { useI18n } from '../../i18n';
import { formatRelativeTime } from '../../utils/format';

interface Account {
  biz: string;
  nickname: string;
  alias: string | null;
  is_disabled: boolean;
  last_synced_at: string | null;
  latest_publish_at: string | null;
  article_count: number;
}

const PAGE_SIZE = 50;

/**
 * What hippo is following from this source, for the admin's own subscriptions.
 *
 * The catalogue is shared, so these are the admin's rows: a full instance-wide
 * channel list belongs in a future page of its own, not in a settings pane.
 * The list pages through the account endpoint instead of truncating at one page.
 */
export function SourceSubscriptionPanel({ source }: { source: string }) {
  const { t } = useI18n();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState('');
  // Switching source or retrying races the request it replaces: only the newest
  // answer may touch the list.
  const requestIdRef = useRef(0);

  const loadPage = useCallback(async (page: number) => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    if (page === 1) setLoading(true);
    else setLoadingMore(true);
    setError('');
    try {
      const query = new URLSearchParams({
        source,
        page: String(page),
        page_size: String(PAGE_SIZE),
      });
      const payload = await apiGet(`/api/account?${query.toString()}`);
      if (requestId !== requestIdRef.current) return;
      const next = (payload.accounts as Account[]) || [];
      setAccounts((previous) => (page === 1 ? next : [...previous, ...next]));
      setTotal(Number(payload.total || 0));
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [source]);

  useEffect(() => {
    queueMicrotask(() => {
      void loadPage(1);
    });
  }, [loadPage]);

  const hasMore = accounts.length < total;

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('admin.source.subscriptions', '订阅')}</h2>
          <p className="muted">
            {t('admin.source.subscriptionsHint', '你订阅的频道；添加与删除在「分组」页。')}
          </p>
        </div>
        <Link className="btn ghost" to="/groups">
          {t('admin.source.manageGroups', '前往分组')}
        </Link>
      </div>

      {loading ? (
        <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>
      ) : accounts.length === 0 ? (
        <div className="muted admin-empty">
          {error
            ? t('admin.source.subscriptionsFailed', 'Failed to load subscriptions.')
            : t('admin.source.noSubscriptions', '还没有订阅。')}
          {error ? (
            <button className="btn ghost" type="button" onClick={() => void loadPage(1)}>
              {t('common.retry', 'Retry')}
            </button>
          ) : null}
        </div>
      ) : (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>{t('groups.title', '名称')}</th>
                <th>{t('accounts.articleCount', '文章')}</th>
                <th>{t('accounts.lastUpdatedLabel', 'Latest publication')}</th>
                <th>{t('accounts.lastSyncedLabel', 'Last sync')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {accounts.map((account) => (
                <tr key={account.biz} className={account.is_disabled ? 'is-disabled' : ''}>
                  <td>
                    {account.nickname}
                    <div className="account-sub admin-mono">{account.biz}</div>
                  </td>
                  <td>{account.article_count}</td>
                  <td className="muted">{formatRelativeTime(account.latest_publish_at, t) || '—'}</td>
                  <td className="muted">{formatRelativeTime(account.last_synced_at, t) || '—'}</td>
                  <td>
                    <Link className="btn ghost" to={`/articles?account=${encodeURIComponent(account.biz)}`}>
                      {t('groups.viewArticles', '查看文章')}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {error ? <p className="muted admin-note">{t('admin.source.subscriptionsFailed', 'Failed to load subscriptions.')}</p> : null}
          {hasMore ? (
            <button
              className="btn ghost"
              type="button"
              disabled={loadingMore}
              onClick={() => void loadPage(accounts.length / PAGE_SIZE + 1)}
            >
              {loadingMore ? t('common.loading', 'Loading…') : t('admin.source.loadMore', 'Load more')}
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
}
