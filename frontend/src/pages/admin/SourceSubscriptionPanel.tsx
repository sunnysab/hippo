import { useCallback, useEffect, useState } from 'react';
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
  article_count: number;
}

/**
 * What hippo is following from this source, for the admin's own subscriptions.
 *
 * The catalogue is shared, so these are the admin's rows: a full instance-wide
 * channel list belongs in a future page of its own, not in a settings pane.
 */
export function SourceSubscriptionPanel({ source }: { source: string }) {
  const { t } = useI18n();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const payload = await apiGet(`/api/account?source=${encodeURIComponent(source)}&page_size=100`);
      setAccounts(((payload.accounts as Account[]) || []));
    } finally {
      setLoading(false);
    }
  }, [source]);

  useEffect(() => {
    void load();
  }, [load]);

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
        <div className="muted admin-empty">{t('admin.source.noSubscriptions', '还没有订阅。')}</div>
      ) : (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>{t('groups.title', '名称')}</th>
                <th>{t('accounts.articleCount', '文章')}</th>
                <th>{t('accounts.lastUpdated', '最近更新')}</th>
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
        </div>
      )}
    </div>
  );
}
