import { memo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { formatRelativeTime } from '../../utils/format';
import { getSourceLabel } from '../../utils/source';
import { hasAccountWork } from '../../utils/sync';
import { AccountDetailsModal } from './AccountDetailsModal';
import type { Account } from '../../store/shared';

interface AccountCardProps {
  account: Account;
}

export const AccountCard = memo(function AccountCard({ account }: AccountCardProps) {
  const { state, dispatch } = useGroupsState();
  const { t } = useI18n();
  const navigate = useNavigate();
  const [showDetails, setShowDetails] = useState(false);
  const published = formatRelativeTime(account.latest_publish_at, t);
  const latestArticle = published
    ? t('accounts.latestArticle', '最新文章 {time}').replace('{time}', published)
    : t('accounts.lastUpdatedEmpty', '暂无发布时间');
  const source = getSourceLabel(account.source || 'weixin', t);
  const syncActive = account.sync ? hasAccountWork(account.sync) : account.backfill_running || account.backfill_pending;
  const syncRunning = account.sync
    ? ['running', 'cancelling'].includes(account.sync.history.status)
      || account.sync.listing?.status === 'running' || account.sync.articles.processing > 0
    : account.backfill_running;
  const syncFailed = Boolean(account.sync && (
    account.sync.history.status === 'failed' || account.sync.listing?.status === 'failed'
    || account.sync.articles.failed > 0 || (account.sync.images?.failed || 0) > 0
  ));
  const handleViewArticles = () => {
    const params = new URLSearchParams();
    if (account.group_id) params.set('group', String(account.group_id));
    params.set('account', account.biz);
    navigate(`/articles?${params.toString()}`);
  };

  return (
    <>
      <div className={`card account-card${account.is_disabled ? ' is-disabled' : ''}`}>
        <input
          className="account-check"
          type="checkbox"
          aria-label={t('accounts.selectAccount', '选择 {name}').replace('{name}', account.nickname)}
          data-biz={account.biz}
          checked={state.selectedAccounts.includes(account.biz)}
          onChange={() => dispatch({ type: 'TOGGLE_SELECTED', biz: account.biz })}
        />
        <img className="account-avatar" src={account.avatar_url} alt="" onError={(event) => { event.currentTarget.style.visibility = 'hidden'; }} />
        <div className="account-card-content">
          <button className="account-name account-name-link" type="button" title={account.nickname} onClick={handleViewArticles}>
            {account.nickname}
          </button>
          <div className="account-sub account-identity" title={account.alias || source}>{source}{account.alias ? ` · ${account.alias}` : ''}</div>
          <div className="account-sub account-summary" aria-live="polite">
            <span>{latestArticle}</span>
            {account.is_disabled && <span className="account-summary-state">{t('accounts.paused', '已暂停')}</span>}
            {syncFailed
              ? <span className="account-summary-state is-warning">{t('accounts.syncFailedShort', '同步有失败')}</span>
              : !account.is_disabled && syncActive && (
                <span className="account-summary-state">
                  {syncRunning && <span className="sync-dot" aria-hidden="true" />}
                  {syncRunning ? t('accounts.syncingShort', '同步中') : t('accounts.syncQueuedShort', '等待同步')}
                </span>
              )}
          </div>
        </div>
        <button
          className="account-details-trigger"
          type="button"
          aria-label={t('accounts.viewDetails', '查看 {name} 的详情').replace('{name}', account.nickname)}
          aria-haspopup="dialog"
          onClick={() => setShowDetails(true)}
        >
          {t('accounts.details', '详情')}
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
        </button>
      </div>
      {showDetails && <AccountDetailsModal account={account} onClose={() => setShowDetails(false)} onViewArticles={handleViewArticles} />}
    </>
  );
});
