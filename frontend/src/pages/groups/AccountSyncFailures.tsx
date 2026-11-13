import { useEffect, useState } from 'react';
import { apiGet, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useGroupsState } from '../../store/groups';
import type { Account, AccountSyncFailures as SyncFailures } from '../../store/shared';
import { formatDateTime } from '../../utils/format';
import { normalizeSafeUrl } from '../../utils/markdown';

export function AccountSyncFailures({ biz, onClose }: { biz: string; onClose: () => void }) {
  const { t } = useI18n();
  const { dispatch } = useGroupsState();
  const [detail, setDetail] = useState<(Account & { sync_failures: SyncFailures }) | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void apiGet(`/api/account/${encodeURIComponent(biz)}`, { signal: controller.signal })
      .then((payload) => {
        if (controller.signal.aborted) return;
        const account = payload as unknown as Account & { sync_failures: SyncFailures };
        setDetail(account);
        dispatch({ type: 'UPDATE_ACCOUNT', biz, patch: { sync: account.sync, last_synced_at: account.last_synced_at } });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted && !isAuthError(err)) setError((err as Error).message || '');
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [biz, retry, dispatch]);

  if (loading) return <p className="account-sub" role="status">{t('sync.loadingDetails', '正在加载同步详情…')}</p>;
  if (error !== null) return <div className="settings-error" role="alert">
    <p>{t('sync.loadDetailsFailed', '加载同步详情失败。')}{error.trim() ? ` ${error}` : ''}</p>
    <button className="btn ghost" type="button" onClick={() => { setLoading(true); setError(null); setRetry((value) => value + 1); }}>
      {t('common.retry', '重试')}
    </button>
  </div>;
  if (!detail) return null;

  const stages = [
    { key: 'articles' as const, label: t('sync.failedBodies', '正文抓取失败') },
    { key: 'images' as const, label: t('sync.failedImages', '图片下载失败') },
  ];
  const listFailed = detail.sync?.history.status === 'failed' || detail.sync?.listing?.status === 'failed';
  const hasFailures = stages.some(({ key }) => detail.sync_failures[key].length > 0);
  if (!hasFailures && !listFailed) return null;

  return <section className="account-dialog-section account-sync-failures">
    <h3>{t('sync.affectedArticles', '受影响的文章')}</h3>
    {listFailed && <p className="account-sub">{t('sync.listFailureImpact', '列表获取失败，可能有文章尚未被发现，暂时无法列出具体文章。')}</p>}
    {hasFailures && <p className="account-sub">{t('sync.recentFailures', '每类显示最近 5 条失败记录；失败项已停止自动重试。')}</p>}
    {stages.map(({ key, label }) => detail.sync_failures[key].length > 0 && <div key={key}>
      <h4>{label}</h4>
      <ul>
        {detail.sync_failures[key].map((item) => {
          const original = normalizeSafeUrl(item.link || '');
          const title = item.title || t('sync.articleIdentifier', '文章标识：{id}').replace('{id}', item.article_id);
          return <li key={item.id}>
            <div className="account-sync-failure-title">
              {item.article_pk
                ? <a href={`#/articles?article=${item.article_pk}`} onClick={onClose}>{title}</a>
                : original ? <a href={original} target="_blank" rel="noopener noreferrer">{title}</a> : <strong>{title}</strong>}
            </div>
            <p className="account-sync-failure-error">{item.error || t('sync.noFailureReason', '未记录具体原因')}</p>
            <p className="account-sub">
              {t('sync.failureAttempts', '已尝试 {n} 次').replace('{n}', String(item.attempts))}
              {item.updated_at && <> · <time dateTime={item.updated_at}>{formatDateTime(item.updated_at)}</time></>}
            </p>
            {original && item.article_pk && <a className="account-sub" href={original} target="_blank" rel="noopener noreferrer">{t('articles.menu.openOriginal', '查看原文')}</a>}
          </li>;
        })}
      </ul>
    </div>)}
  </section>;
}
